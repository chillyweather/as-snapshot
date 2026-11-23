from __future__ import annotations

import csv
import json
from pathlib import Path

from state_store_support import read_state
from state_store_support import seed_state

from autoscout.pipeline import run_pipeline

RUN_DATE = "2026-07-29"


def _candidate(title: str, author: str, signal_strength: str) -> dict[str, object]:
    return {
        "title": title,
        "primary_authors": [author],
        "candidate_category": "history",
        "signal_strength": signal_strength,
        "evidence_note": f"{title} appears in the configured fixture.",
        "publisher_fit": "clear fit",
        "russian_edition_status": "not found in Russian",
        "visible_risk_status": "no visible risk",
    }


def _source(name: str, family: str, candidates: list[dict[str, object]]) -> dict[str, object]:
    return {
        "name": name,
        "family": family,
        "url": f"https://example.test/{name.lower().replace(' ', '-')}",
        "access_status": "open",
        "extraction_method": "fixture",
        "check_frequency": "weekly",
        "fixture_candidates": candidates,
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def test_opt_in_review_keeps_canonical_order_and_explains_shadow_movement(tmp_path: Path) -> None:
    durable = _candidate("Durable Discovery", "Ada Archive", "winner")
    new_signal_candidate = _candidate("New Signal First", "Nora Novelty", "shortlist")
    config_path = tmp_path / "config.json"
    _write_json(
        config_path,
        {
            "run_date": RUN_DATE,
            "search_optimization_trial": True,
            "sources": [
                _source("Fixture Awards", "award", [durable, new_signal_candidate]),
                _source("Fixture Media", "media/curated list", [durable]),
            ],
        },
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    seed_state(
        state_dir,
        candidate_history=[
            {
                "identity_key": "durable discovery|ada archive",
                "title": "Durable Discovery",
                "primary_authors": ["Ada Archive"],
                "editor_status": "interested",
                "editor_note": "Keep this wording exactly.",
                "last_seen_date": "2026-07-22",
                "source_evidence": [
                    {
                        "source_name": "Fixture Awards",
                        "source_family": "award",
                        "signal_strength": "winner",
                    },
                    {
                        "source_name": "Fixture Media",
                        "source_family": "media/curated list",
                        "signal_strength": "major media list",
                    },
                ],
                "independent_signal_families": ["award", "media/curated list"],
                "primary_signal_strength": "winner",
                "priority_label": "A",
                "russian_edition_status": "not found in Russian",
                "visible_risk_status": "no visible risk",
            }
        ],
    )
    output_dir = tmp_path / "run"

    run_pipeline(config_path, output_dir, state_dir)

    with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
        canonical_titles = [row["Title"] for row in csv.DictReader(handle)]
    assert canonical_titles == ["New Signal First", "Durable Discovery"]

    review = json.loads((output_dir / "search-optimization-review.json").read_text(encoding="utf-8"))
    assert review["schema_version"] == 4
    assert review["run_identity"] == RUN_DATE
    assert review["canonical_effect"] == "none"
    assert review["ordering_method"]["kind"] == "deterministic lexicographic"
    assert review["ordering_method"]["opaque_score"] is False

    candidates = {candidate["title"]: candidate for candidate in review["candidates"]}
    assert candidates["Durable Discovery"]["identity_key"] == "durable discovery|ada archive"
    assert candidates["Durable Discovery"]["canonical_rank"] == 2
    assert candidates["Durable Discovery"]["shadow_rank"] == 1
    assert candidates["Durable Discovery"]["rank_delta"] == 1
    assert candidates["Durable Discovery"]["canonical_priority_label"] == "B"
    assert candidates["Durable Discovery"]["editor_status"] == "interested"
    assert candidates["Durable Discovery"]["outcome_cohort"] == "strong_interest"
    assert candidates["Durable Discovery"]["independent_signal_families"] == [
        "award",
        "media/curated list",
    ]
    ses = candidates["Durable Discovery"]["source_evidence_summary"]
    assert ses["evidence_count"] == 2
    assert ses["source_names"] == ["Fixture Awards", "Fixture Media"]
    assert ses["independent_signal_family_count"] == 2
    assert ses["strongest_signal_strength"] == "winner"
    # Source outcome is present but may be neutral for small fixtures.
    assert "source_outcome" in ses
    assert candidates["Durable Discovery"]["explanations"]
    assert "New Signal First" in " ".join(candidates["Durable Discovery"]["explanations"])
    assert "canonical update digest" in " ".join(candidates["Durable Discovery"]["explanations"])
    assert "Priority Label B ranks before Priority Label C" in " ".join(candidates["Durable Discovery"]["explanations"])
    assert "opaque score" not in " ".join(candidates["Durable Discovery"]["explanations"]).lower()
    for candidate in review["candidates"]:
        if candidate["rank_delta"] != 0:
            assert candidate["explanations"]

    markdown = (output_dir / "search-optimization-review.md").read_text(encoding="utf-8")
    assert f"Run: {RUN_DATE}" in markdown
    assert "Schema version: 4" in markdown
    assert "Durable Discovery" in markdown
    assert "Ada Archive" in markdown
    assert "durable discovery&#124;ada archive" in markdown
    assert "2 evidence records" in markdown
    assert "Fixture Awards, Fixture Media" in markdown
    assert "strongest: winner" in markdown
    assert "Interested - strong interest" in markdown
    assert "read-only" in markdown.lower()


def _run_outcome_fixture(
    root: Path,
    editor_note_suffix: str,
    *,
    trial_enabled: bool = True,
) -> tuple[Path, Path]:
    root.mkdir()
    status_candidates = [
        ("Interested Evidence", "Interest Author", "interested"),
        ("Analysis Evidence", "Analysis Author", "needs_deeper_analysis"),
        ("Watch Evidence", "Watch Author", "maybe_watch"),
        ("Known Evidence", "Known Author", "already_known"),
        ("Negative Evidence", "Negative Author", "reject"),
    ]
    candidates = [_candidate(title, author, "winner") for title, author, _status in status_candidates]
    candidates.extend(
        [
            _candidate("Hidden By Rule", "Hidden Author", "winner"),
            _candidate("Hidden By Editor", "Invisible Author", "winner"),
            {
                **_candidate("Risk Floor", "Risk Author", "winner"),
                "visible_risk_status": "clear visible risk",
                "risk_signal_scope": "book-content",
                "risk_evidence_url": "https://example.test/risk-floor",
            },
        ]
    )
    config_path = root / "config.json"
    _write_json(
        config_path,
        {
            "run_date": RUN_DATE,
            "search_optimization_trial": trial_enabled,
            "sources": [_source("Fixture Awards", "award", candidates)],
        },
    )
    state_dir = root / "state"
    state_dir.mkdir()
    history = []
    for title, author, status in status_candidates:
        history.append(
            {
                "identity_key": f"{title.lower()}|{author.lower()}",
                "title": title,
                "primary_authors": [author],
                "editor_status": status,
                "editor_note": f"Verbatim {editor_note_suffix}: winner, award, media.",
                "last_seen_date": "2026-07-22",
                "source_evidence": [
                    {
                        "source_name": "Fixture Awards",
                        "source_family": "award",
                        "signal_strength": "minor mention",
                    }
                ],
                "independent_signal_families": ["award"],
                "primary_signal_strength": "minor mention",
                "priority_label": "C",
                "russian_edition_status": "not found in Russian",
                "visible_risk_status": "no visible risk",
            }
        )
    seed_state(state_dir, candidate_history=history)
    seed_state(
        state_dir,
        editorial_profile={
            "entries": [
                {
                    "id": "taste-hard-hidden-author",
                    "kind": "hard_rule",
                    "scope": "author",
                    "subject": "Hidden Author",
                    "verbatim_quote": "Never show this author.",
                    "provenance": "editor chat",
                    "created_at": "2026-07-20",
                    "created_by": "agent",
                    "status": "active",
                },
                {
                    "id": "taste-soft-risk-author",
                    "kind": "soft_preference",
                    "scope": "author",
                    "subject": "Risk Author",
                    "direction": "positive",
                    "verbatim_quote": "More books by Risk Author.",
                    "provenance": "editor chat",
                    "created_at": "2026-07-20",
                    "created_by": "agent",
                    "status": "active",
                },
            ]
        },
    )
    seed_state(
        state_dir, candidate_visibility=[{"identity_key": "hidden by editor|invisible author", "visibility": "hidden"}]
    )
    output_dir = root / "run"
    run_pipeline(config_path, output_dir, state_dir)
    return output_dir, state_dir


def test_review_preserves_cohorts_gates_risk_floor_notes_and_determinism(tmp_path: Path) -> None:
    first_output, first_state = _run_outcome_fixture(tmp_path / "first", "note one")
    second_output, second_state = _run_outcome_fixture(tmp_path / "second", "different note")
    disabled_output, disabled_state = _run_outcome_fixture(
        tmp_path / "disabled",
        "note one",
        trial_enabled=False,
    )

    first_bytes = (first_output / "search-optimization-review.json").read_bytes()
    second_bytes = (second_output / "search-optimization-review.json").read_bytes()
    assert first_bytes == second_bytes
    assert (first_output / "search-optimization-review.md").read_bytes() == (
        second_output / "search-optimization-review.md"
    ).read_bytes()

    review = json.loads(first_bytes)
    cohort_by_title = {candidate["title"]: candidate["outcome_cohort"] for candidate in review["candidates"]}
    assert cohort_by_title["Interested Evidence"] == "strong_interest"
    assert cohort_by_title["Analysis Evidence"] == "actionable_follow_up"
    assert cohort_by_title["Watch Evidence"] == "watch"
    assert cohort_by_title["Known Evidence"] == "known_workflow_item"
    assert cohort_by_title["Negative Evidence"] == "negative"
    assert cohort_by_title["Risk Floor"] == "unreviewed"
    assert review["outcome_cohort_counts"] == {
        "strong_interest": 1,
        "actionable_follow_up": 1,
        "watch": 1,
        "known_workflow_item": 1,
        "negative": 1,
        "unreviewed": 1,
    }

    artifact_titles = set(cohort_by_title)
    assert "Hidden By Rule" not in artifact_titles
    assert "Hidden By Editor" not in artifact_titles
    with (first_output / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
        assert artifact_titles == {row["Title"] for row in csv.DictReader(handle)}

    risk = next(candidate for candidate in review["candidates"] if candidate["title"] == "Risk Floor")
    assert risk["canonical_priority_label"] == "D"
    assert "Visible Risk floors" in review["ordering_method"]["canonical_constraints_preserved"]
    assert "accepted Soft Preference effects" in review["ordering_method"]["canonical_constraints_preserved"]

    first_history = read_state(first_state, "candidate_history")
    second_history = read_state(second_state, "candidate_history")
    assert (
        next(record for record in first_history if record["title"] == "Interested Evidence")["editor_note"]
        == "Verbatim note one: winner, award, media."
    )
    assert (
        next(record for record in second_history if record["title"] == "Interested Evidence")["editor_note"]
        == "Verbatim different note: winner, award, media."
    )
    assert "note one" not in first_bytes.decode("utf-8")
    assert "different note" not in second_bytes.decode("utf-8")

    assert not (disabled_output / "search-optimization-review.json").exists()
    assert not (disabled_output / "search-optimization-review.md").exists()
    disabled_artifacts = {path.name: path.read_bytes() for path in disabled_output.iterdir() if path.is_file()}
    enabled_canonical_artifacts = {
        path.name: path.read_bytes()
        for path in first_output.iterdir()
        if path.is_file() and not path.name.startswith("search-optimization-review.")
    }
    assert enabled_canonical_artifacts == disabled_artifacts

    disabled_state_files = {path.name: path.read_bytes() for path in disabled_state.iterdir() if path.is_file()}
    enabled_state_files = {path.name: path.read_bytes() for path in first_state.iterdir() if path.is_file()}
    assert enabled_state_files == disabled_state_files


def test_disabled_trial_removes_stale_optimization_artifacts_from_reused_output_dir(tmp_path: Path) -> None:
    output_dir = tmp_path / "run"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    output_dir.mkdir()
    seed_state(state_dir, candidate_history=[])
    seed_state(state_dir, editorial_profile={"entries": []})
    seed_state(state_dir, candidate_visibility=[])

    # First run: trial enabled -- produces optimization artifacts.
    enabled_config = tmp_path / "enabled.json"
    _write_json(
        enabled_config,
        {
            "run_date": "2026-08-01",
            "search_optimization_trial": True,
            "sources": [_source("Fixture Awards", "award", [_candidate("Book One", "Author One", "winner")])],
        },
    )
    run_pipeline(enabled_config, output_dir, state_dir)
    assert (output_dir / "search-optimization-review.json").exists()
    assert (output_dir / "search-optimization-review.md").exists()

    # Second run: trial disabled with new effective date into the SAME output directory.
    disabled_config = tmp_path / "disabled.json"
    _write_json(
        disabled_config,
        {
            "run_date": "2026-08-01",
            "search_optimization_trial": False,
            "sources": [_source("Fixture Awards", "award", [_candidate("Book Two", "Author Two", "shortlist")])],
        },
    )
    run_pipeline(disabled_config, output_dir, state_dir, effective_date="2026-08-08")
    assert not (output_dir / "search-optimization-review.json").exists()
    assert not (output_dir / "search-optimization-review.md").exists()

    with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
        titles = [row["Title"] for row in csv.DictReader(handle)]
    assert titles == ["Book Two"]
