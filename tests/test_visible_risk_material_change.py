"""An unconcluded Visible Risk screen is not a Material Change.

Since issue #714 the screen concludes only from a description, and at
pipeline time most candidates do not carry their Content Description yet, so
their status is blank with ``partial`` coverage. Comparing that blank with the
stored verdict labelled most of every run "changed" and kept rejected books in
the review. A blank status is unknown: it changes nothing, Candidate History
keeps the previous verdict, and the post-enrichment rescreen labels a status
it concludes that differs from that verdict.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout.cli.weekly import run_mvp
from autoscout.pipeline import refresh_post_enrichment_artifacts
from autoscout.review_rendering import render_review
from autoscout.scouting_candidates import ScoutingCandidate

FIRST_RUN = "2026-06-15"
SECOND_RUN = "2026-06-22"
TITLE = "The Quiet Ledger"


def _book(**fields: Any) -> dict[str, Any]:
    return {
        "title": TITLE,
        "primary_authors": ["Quiet Example"],
        "source_url": "https://example.test/books/the-quiet-ledger",
        "signal_strength": "major media list",
        "candidate_category": "history",
        "evidence_note": "National Book Award for Nonfiction 2025 longlist",
        **fields,
    }


SCREENED_BOOK = _book(short_description="A social history of a port city and its dock workers.")
UNDESCRIBED_BOOK = _book()


def _run(tmp_path: Path, run_date: str, book: dict[str, Any]) -> Path:
    config_path = tmp_path / "sources.json"
    source = {
        "name": "Fixture Agent List",
        "family": "media/curated list",
        "url": "https://example.test/agent-list",
        "access_status": "open",
        "extraction_method": "fixture",
        "check_frequency": "weekly",
        "language": "en",
        "notes": "",
        "fixture_candidates": [book],
    }
    config_path.write_text(json.dumps({"run_date": run_date, "sources": [source]}), encoding="utf-8")
    # The weekly launcher uses one directory for output and state.
    state_dir = tmp_path / "runs"
    run_mvp(config_path, state_dir, state_dir)
    return state_dir


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _set_history_field(state_dir: Path, field: str, value: Any) -> None:
    history = read_state(state_dir, "candidate_history")
    for record in history:
        record[field] = value
    seed_state(state_dir, candidate_history=history)


def _publish_description(state_dir: Path, description: str) -> None:
    """Stand in for description enrichment, which runs after the pipeline."""
    records = _read(state_dir / "scouting-candidates.json")
    for record in records:
        record["content_description"] = description
    _write(state_dir / "scouting-candidates.json", records)
    history = read_state(state_dir, "candidate_history")
    for record in history:
        record["content_description"] = description
    seed_state(state_dir, candidate_history=history)


def _candidate(state_dir: Path, name: str = "scouting-candidates.json") -> dict[str, Any]:
    """The single candidate of the current snapshot, or of the latest snapshot when ``name`` is ``"latest"``."""
    (candidate,) = read_state(state_dir, "latest_scouting_candidates") if name == "latest" else _read(state_dir / name)
    return candidate


def _second_run_without_description(tmp_path: Path) -> Path:
    state_dir = _run(tmp_path, FIRST_RUN, SCREENED_BOOK)
    assert _candidate(state_dir)["visible_risk_status"] == "no visible risk"
    state_dir = _run(tmp_path, SECOND_RUN, UNDESCRIBED_BOOK)
    candidate = _candidate(state_dir)
    assert candidate["visible_risk_status"] == ""
    assert candidate["visible_risk_screen_coverage"]["coverage"] == "partial"
    return state_dir


def test_a_blank_current_status_is_not_a_risk_change() -> None:
    history = {"visible_risk_status": "no visible risk"}
    candidate = ScoutingCandidate({"visible_risk_status": ""})

    assert candidate.material_change_reasons(history) == []


def test_a_concluded_status_that_differs_is_still_a_risk_change() -> None:
    history = {"visible_risk_status": "no visible risk"}
    candidate = ScoutingCandidate({"visible_risk_status": "text check required"})

    assert candidate.material_change_reasons(history) == ["Visible Risk status changed"]


def test_the_russian_status_comparison_is_unchanged() -> None:
    history = {"russian_edition_status": "Russian edition found"}

    assert ScoutingCandidate({"russian_edition_status": ""}).material_change_reasons(history) == [
        "Russian status changed"
    ]


def test_a_rejected_book_without_a_description_stays_suppressed(tmp_path: Path) -> None:
    state_dir = _run(tmp_path, FIRST_RUN, SCREENED_BOOK)
    _set_history_field(state_dir, "editor_status", "reject")

    state_dir = _run(tmp_path, SECOND_RUN, UNDESCRIBED_BOOK)

    assert _read(state_dir / "scouting-candidates.json") == []
    (suppressed,) = _read(state_dir / "suppressed-candidates.json")
    assert suppressed["title"] == TITLE
    assert suppressed["suppression_reason"] == "suppressed by editor status without Material Change"


def test_history_keeps_the_previous_verdict_while_the_screen_is_unconcluded(tmp_path: Path) -> None:
    state_dir = _second_run_without_description(tmp_path)

    (record,) = read_state(state_dir, "candidate_history")
    assert record["visible_risk_status"] == "no visible risk"
    assert record["visible_risk_screen_coverage"]["coverage"] == "screened"
    candidate = _candidate(state_dir)
    assert candidate["update_delta"] == "unchanged"
    assert "material_change_reasons" not in candidate


def test_an_unchanged_verdict_after_the_rescreen_stays_unchanged(tmp_path: Path) -> None:
    state_dir = _second_run_without_description(tmp_path)
    _publish_description(state_dir, "Книга о жизни портового города и его жителей.")

    assert refresh_post_enrichment_artifacts(tmp_path / "sources.json", state_dir, state_dir)["status"] == "ok"

    for name in ("scouting-candidates.json", "latest"):
        candidate = _candidate(state_dir, name)
        assert candidate["visible_risk_status"] == "no visible risk"
        assert candidate["update_delta"] == "unchanged"
        assert "material_change_reasons" not in candidate
    (record,) = read_state(state_dir, "candidate_history")
    assert "last_material_change_date" not in record
    assert _read(state_dir / "run-summary.json")["update_delta_counts"]["unchanged"] == 1


def test_a_risk_concluded_after_enrichment_is_labelled_in_every_copy(tmp_path: Path) -> None:
    state_dir = _second_run_without_description(tmp_path)
    _publish_description(state_dir, "A history of the Kremlin and its rulers.")

    assert refresh_post_enrichment_artifacts(tmp_path / "sources.json", state_dir, state_dir)["status"] == "ok"

    for name in ("scouting-candidates.json", "latest"):
        candidate = _candidate(state_dir, name)
        assert candidate["visible_risk_status"] == "text check required"
        assert candidate["update_delta"] == "changed"
        assert candidate["material_change_reasons"] == ["Visible Risk status changed"]
    (record,) = read_state(state_dir, "candidate_history")
    assert record["visible_risk_status"] == "text check required"
    assert record["last_material_change_date"] == SECOND_RUN
    assert "update_delta" not in record
    assert "material_change_reasons" not in record
    assert _read(state_dir / "run-summary.json")["update_delta_counts"]["changed"] == 1

    assert render_review(state_dir) == 0
    with (state_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
        (row,) = list(csv.DictReader(handle))
    assert row["Update delta"] == "changed"


def test_a_repeated_refresh_changes_nothing(tmp_path: Path) -> None:
    state_dir = _second_run_without_description(tmp_path)
    _publish_description(state_dir, "A history of the Kremlin and its rulers.")
    config_path = tmp_path / "sources.json"
    refresh_post_enrichment_artifacts(config_path, state_dir, state_dir)
    names = ("scouting-candidates.json", "latest-scouting-candidates.json")
    before = {name: (state_dir / name).read_bytes() for name in names}
    history_before = state_snapshot(state_dir)

    refresh_post_enrichment_artifacts(config_path, state_dir, state_dir)

    assert {name: (state_dir / name).read_bytes() for name in names} == before
    assert state_snapshot(state_dir) == history_before
