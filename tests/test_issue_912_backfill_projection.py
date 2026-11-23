"""Issue #912: a description backfill reaches the snapshots and says what it is.

``apply-description-backfill`` used to write only Candidate History, so the
current and latest Scouting Candidates kept the replaced stub until the next
weekly run. A backfill listing two research sources also rendered as
grounded by two sources, although no stage had checked those pages.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout.__main__ import main
from autoscout.content_description import DESCRIPTION_BACKFILLED_STATUS
from autoscout.content_description import DESCRIPTION_GROUNDED_STATUS
from autoscout.content_description import DESCRIPTION_SINGLE_SOURCE_STATUS
from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.review_rendering import assemble_cumulative_review

IDENTITY_KEY = "the stub book|alex example"
OTHER_KEY = "the zebra book|sam example"
STUB_TEXT = "Книга посвящена теме, обозначенной в заглавии «The Stub Book». Автор предлагает обзор предмета."
RESEARCHED_TEXT = (
    "Историк восстанавливает, как в портовых городах Средиземноморья складывались первые страховые общества, "
    "и показывает, как купцы делили риск кораблекрушений задолго до появления современных компаний. "
    "Автор опирается на нотариальные записи и переписку торговых домов."
)
OTHER_TEXT = "Другая книга с собственным описанием, которое проекция не должна трогать ни при каких условиях."


def _stub_record() -> dict[str, Any]:
    return {
        "candidate_id": "candidate-stub",
        "identity_key": IDENTITY_KEY,
        "title": "The Stub Book",
        "primary_authors": ["Alex Example"],
        "content_description": STUB_TEXT,
        "description_provenance": {
            "provenance": "codex-generated",
            "identity_key": IDENTITY_KEY,
            "content_description": STUB_TEXT,
            "applied_date": "2026-11-02",
        },
    }


def _other_record() -> dict[str, Any]:
    return {
        "candidate_id": "candidate-other",
        "identity_key": OTHER_KEY,
        "title": "The Zebra Book",
        "primary_authors": ["Sam Example"],
        "content_description": OTHER_TEXT,
        "description_provenance": {"provenance": "grounded", "identity_key": OTHER_KEY},
    }


def _snapshot_row(record: dict[str, Any], rank: str) -> dict[str, Any]:
    return {**record, "current_rank": rank, "source_evidence": [{"source_name": "Example Chart"}]}


def _state(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    seed_state(state_dir, candidate_history=[_stub_record(), _other_record()])
    snapshot = [_snapshot_row(_stub_record(), "3"), _snapshot_row(_other_record(), "7")]
    write_json(state_dir / "scouting-candidates.json", snapshot)
    seed_state(state_dir, latest_scouting_candidates=snapshot)
    return state_dir


def _apply(tmp_path: Path, state_dir: Path, sources: list[str]) -> int:
    artifacts = tmp_path / "backfill.json"
    write_json(
        artifacts,
        {
            "backfill_items": [
                {"identity_key": IDENTITY_KEY, "content_description": RESEARCHED_TEXT, "research_sources": sources}
            ]
        },
    )
    return main(["apply-description-backfill", "--state-dir", str(state_dir), "--artifacts-file", str(artifacts)])


def test_the_backfill_reaches_both_snapshots(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)

    assert _apply(tmp_path, state_dir, ["https://example.test/a", "https://example.test/b"]) == 0

    copies = {
        "candidate_history": read_state(state_dir, "candidate_history"),
        "scouting-candidates.json": read_json(state_dir / "scouting-candidates.json"),
        "latest_scouting_candidates": read_state(state_dir, "latest_scouting_candidates"),
    }
    for name, records in copies.items():
        rows = {row["identity_key"]: row for row in records}
        assert rows[IDENTITY_KEY]["content_description"] == RESEARCHED_TEXT, name
        assert rows[IDENTITY_KEY]["description_provenance"]["provenance"] == "backfilled", name
        assert rows[OTHER_KEY]["content_description"] == OTHER_TEXT, name


def test_the_projection_keeps_membership_order_and_unrelated_fields(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)

    _apply(tmp_path, state_dir, ["https://example.test/a"])

    rows = read_state(state_dir, "latest_scouting_candidates")
    assert [row["identity_key"] for row in rows] == [IDENTITY_KEY, OTHER_KEY]
    assert [row["current_rank"] for row in rows] == ["3", "7"]
    assert rows[0]["source_evidence"] == [{"source_name": "Example Chart"}]


def test_a_repeated_backfill_leaves_the_snapshots_byte_identical(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)
    _apply(tmp_path, state_dir, ["https://example.test/a"])
    first_current = (state_dir / "scouting-candidates.json").read_bytes()
    first_latest = state_snapshot(state_dir)["latest_scouting_candidates"]

    _apply(tmp_path, state_dir, ["https://example.test/a"])

    assert (state_dir / "scouting-candidates.json").read_bytes() == first_current
    assert state_snapshot(state_dir)["latest_scouting_candidates"] == first_latest


def test_a_missing_snapshot_is_not_created(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)
    (state_dir / "scouting-candidates.json").unlink()

    assert _apply(tmp_path, state_dir, ["https://example.test/a"]) == 0

    assert not (state_dir / "scouting-candidates.json").exists()
    rows = {row["identity_key"]: row for row in read_state(state_dir, "latest_scouting_candidates")}
    assert rows[IDENTITY_KEY]["content_description"] == RESEARCHED_TEXT


def _review_status(state_dir: Path) -> str:
    history = read_state(state_dir, "candidate_history")
    current = read_json(state_dir / "scouting-candidates.json")
    stale_stage = {
        "outcomes": [
            {
                "candidate_id": "candidate-stub",
                "identity_key": IDENTITY_KEY,
                "outcome": "attempted_no_description",
                "reason_code": "search_or_fetch_failure",
            }
        ]
    }
    rows, _summary = assemble_cumulative_review(
        current_candidates=current,
        candidate_history=history,
        current_editor_rows=[],
        grounded_description_stage=stale_stage,
    )
    (row,) = [row for row in rows if row["identity_key"] == IDENTITY_KEY]
    assert row["content_description"] == RESEARCHED_TEXT
    return str(row["description_status"])


def test_a_backfill_with_two_research_sources_is_not_shown_as_grounded(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)
    _apply(tmp_path, state_dir, ["https://example.test/a", "https://example.test/b"])

    status = _review_status(state_dir)

    assert status == DESCRIPTION_BACKFILLED_STATUS
    assert status not in {DESCRIPTION_GROUNDED_STATUS, DESCRIPTION_SINGLE_SOURCE_STATUS}


def test_a_backfill_with_one_research_source_is_not_shown_as_single_source(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)
    _apply(tmp_path, state_dir, ["https://example.test/a"])

    assert _review_status(state_dir) == DESCRIPTION_BACKFILLED_STATUS


def test_prepare_lists_codex_generated_stubs(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)
    output = tmp_path / "work.json"

    assert main(["prepare-description-backfill", "--state-dir", str(state_dir), "--output-file", str(output)]) == 0

    keys = [item["identity_key"] for item in read_json(output)["work_items"]]
    assert keys == [IDENTITY_KEY]
