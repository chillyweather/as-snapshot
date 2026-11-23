"""Issue #917: a merge can settle different editor decisions, and a merged row is no longer new.

On 2026-11-02 Christopher Lloyd's memoir existed three times: rejected as
"IN MY OWN TIME", marked for deeper analysis as "Glad I'm Still Alive", and
new as "I'm Glad I'm Still Alive". The three-way merge was refused with no
way to resolve the decisions, and the two-way merge left the row in «Новые».
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_databases

from autoscout.__main__ import main
from autoscout.identity_corrections import EDITOR_DECISION_RESOLUTION_RECORD_FIELD
from autoscout.identity_corrections import apply_identity_correction

OWN_TIME = "in my own time|christopher lloyd"
GLAD = "glad i m still alive|christopher lloyd"
IM_GLAD = "i m glad i m still alive|christopher lloyd"
REJECT_NOTE = "[editor note withheld]."
DEEPER_NOTE = "Кристофер Ллойд узнаваем по «Назад в будущее»; стоит оценить полный текст."


def _history() -> list[dict[str, Any]]:
    return [
        {
            "candidate_id": "candidate-own-time",
            "identity_key": OWN_TIME,
            "title": "IN MY OWN TIME",
            "primary_authors": ["Christopher Lloyd"],
            "first_seen_date": "2026-10-15",
            "last_seen_date": "2026-11-01",
            "editor_status": "reject",
            "editor_note": REJECT_NOTE,
            "priority_label": "C",
            "independent_signal_families": ["media/curated list"],
            "independent_signal_count": 1,
            "source_evidence": [{"source_name": "Chart", "signal_strength": "bestseller list"}],
        },
        {
            "candidate_id": "candidate-glad",
            "identity_key": GLAD,
            "title": "Glad I'm Still Alive",
            "primary_authors": ["Christopher Lloyd"],
            "first_seen_date": "2026-10-29",
            "last_seen_date": "2026-10-29",
            "editor_status": "needs_deeper_analysis",
            "editor_note": DEEPER_NOTE,
            "priority_label": "C",
            "independent_signal_families": ["media/curated list"],
            "independent_signal_count": 1,
            "source_evidence": [{"source_name": "PEOPLE", "signal_strength": "media mention"}],
        },
        {
            "candidate_id": "candidate-im-glad",
            "identity_key": IM_GLAD,
            "title": "I’m Glad I’m Still Alive",
            "primary_authors": ["Christopher Lloyd"],
            "first_seen_date": "2026-11-02",
            "last_seen_date": "2026-11-02",
            "priority_label": "C",
            "independent_signal_families": ["media/curated list"],
            "independent_signal_count": 1,
            "source_evidence": [{"source_name": "PEOPLE", "signal_strength": "media mention"}],
        },
    ]


def _today_row() -> dict[str, Any]:
    return {
        "candidate_id": "candidate-im-glad",
        "identity_key": IM_GLAD,
        "title": "I’m Glad I’m Still Alive",
        "primary_authors": ["Christopher Lloyd"],
        "update_delta": "new",
        "priority_label": "C",
        "independent_signal_families": ["media/curated list"],
        "independent_signal_count": 1,
        "source_evidence": [{"source_name": "PEOPLE", "signal_strength": "media mention"}],
    }


def _state(tmp_path: Path) -> Path:
    seed_state(tmp_path, candidate_history=_history(), latest_scouting_candidates=[_today_row()])
    (tmp_path / "scouting-candidates.json").write_text(json.dumps([_today_row()], ensure_ascii=False), encoding="utf-8")
    return tmp_path


def _snapshot(state_dir: Path) -> dict[str, bytes]:
    return {**{path.name: path.read_bytes() for path in sorted(state_dir.iterdir())}, **state_databases()}


def _observed() -> dict[str, Any]:
    return {
        OWN_TIME: {"editor_status": "reject", "editor_note": REJECT_NOTE},
        GLAD: {"editor_status": "needs_deeper_analysis", "editor_note": f"  {DEEPER_NOTE}  "},
    }


def _correction(keys: list[str], *, chosen: str = GLAD, observed: Any = None, resolve: bool = True) -> dict[str, Any]:
    correction: dict[str, Any] = {
        "source_identity_keys": keys,
        "canonical": {"identity_key": GLAD, "title": "Glad I'm Still Alive", "primary_authors": ["Christopher Lloyd"]},
    }
    if resolve:
        correction["editor_decision_resolution"] = {
            "canonical_source_identity_key": chosen,
            "observed_editor_decisions": _observed() if observed is None else observed,
        }
    return correction


ALL = [OWN_TIME, GLAD, IM_GLAD]


def test_without_a_resolution_different_decisions_still_refuse_the_merge(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)
    before = _snapshot(state_dir)
    with pytest.raises(ValueError, match="conflicting editor_status"):
        apply_identity_correction(state_dir, _correction(ALL, resolve=False))
    assert _snapshot(state_dir) == before


def test_a_complete_resolution_merges_and_keeps_every_other_decision_verbatim(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)

    report = apply_identity_correction(state_dir, _correction(ALL))

    (merged,) = read_state(state_dir, "candidate_history")
    assert merged["identity_key"] == GLAD
    assert merged["editor_status"] == "needs_deeper_analysis" and merged["editor_note"] == DEEPER_NOTE
    assert merged[EDITOR_DECISION_RESOLUTION_RECORD_FIELD] == {
        "canonical_identity_key": GLAD,
        "chosen_source_identity_key": GLAD,
        "discarded_decisions": [
            {"source_identity_key": OWN_TIME, "editor_status": "reject", "editor_note": REJECT_NOTE}
        ],
    }
    assert merged["first_seen_date"] == "2026-10-15"
    assert report["editor_decision_resolution"]["chosen_source_identity_key"] == GLAD


@pytest.mark.parametrize(
    ("observed", "message"),
    [
        ({GLAD: _observed()[GLAD]}, "no observed decision for in my own time"),
        ({**_observed(), IM_GLAD: {"editor_status": "reject"}}, "record without a decision"),
        (
            {**_observed(), OWN_TIME: {"editor_status": "reject", "editor_note": "Другая заметка"}},
            "the decision changed for in my own time",
        ),
        ({**_observed(), OWN_TIME: {"editor_status": "maybe_watch", "editor_note": REJECT_NOTE}}, "decision changed"),
        ({**_observed(), "nobody|nowhere": {"editor_status": "reject"}}, "unknown source identity key"),
    ],
)
def test_a_missing_extra_or_stale_entry_fails_before_any_write(tmp_path: Path, observed: Any, message: str) -> None:
    state_dir = _state(tmp_path)
    before = _snapshot(state_dir)
    with pytest.raises(ValueError, match=message):
        apply_identity_correction(state_dir, _correction(ALL, observed=observed))
    assert _snapshot(state_dir) == before


def test_the_chosen_record_must_carry_a_decision(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)
    with pytest.raises(ValueError, match="must choose a record that has an editor decision"):
        apply_identity_correction(state_dir, _correction(ALL, chosen=IM_GLAD))


def test_a_chosen_decision_without_a_note_cannot_set_aside_one_with_a_note(tmp_path: Path) -> None:
    # The review shows the editor-facing CSV note when a record has none, so
    # the discarded note would come back beside the chosen status (review of #917).
    state_dir = _state(tmp_path)
    history = _history()
    del history[0]["editor_note"]
    seed_state(state_dir, candidate_history=history)
    before = _snapshot(state_dir)
    observed = {**_observed(), OWN_TIME: {"editor_status": "reject"}}
    with pytest.raises(ValueError, match="without editor_note over one that has it"):
        apply_identity_correction(state_dir, _correction(ALL, chosen=OWN_TIME, observed=observed))
    assert _snapshot(state_dir) == before


def test_a_merged_row_is_not_new_when_its_work_was_seen_before(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)

    apply_identity_correction(state_dir, _correction([GLAD, IM_GLAD], resolve=False))

    snapshots = {
        "latest_scouting_candidates": read_state(state_dir, "latest_scouting_candidates"),
        "scouting-candidates.json": json.loads((state_dir / "scouting-candidates.json").read_text(encoding="utf-8")),
    }
    for name, rows in snapshots.items():
        (row,) = rows
        assert row["identity_key"] == GLAD
        assert row["update_delta"] in {"unchanged", "changed", "upgraded"}, name
    assert "editor_status" not in row


def test_a_row_merged_into_nothing_older_keeps_its_label(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)
    history = _history()
    history[1]["first_seen_date"] = "2026-11-03"
    seed_state(state_dir, candidate_history=history)
    labelled = deepcopy(_today_row())
    labelled["update_delta"] = "changed"
    (state_dir / "scouting-candidates.json").write_text(json.dumps([labelled], ensure_ascii=False), encoding="utf-8")

    apply_identity_correction(state_dir, _correction([GLAD, IM_GLAD], resolve=False))

    (latest,) = read_state(state_dir, "latest_scouting_candidates")
    (current,) = json.loads((state_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
    assert latest["update_delta"] == "new"
    assert current["update_delta"] == "changed"


def test_the_dry_run_prints_the_decision_choice_and_writes_nothing(tmp_path: Path, capsys: Any) -> None:
    state_dir = _state(tmp_path)
    before = _snapshot(state_dir)
    correction_file = tmp_path.parent / "correction-917.json"
    correction_file.write_text(json.dumps(_correction(ALL), ensure_ascii=False), encoding="utf-8")

    code = main(
        [
            "merge-candidate-identity",
            "--state-dir",
            str(state_dir),
            "--correction-file",
            str(correction_file),
            "--dry-run",
        ]
    )

    out = capsys.readouterr().out
    assert code == 0 and _snapshot(state_dir) == before
    assert "editor_decision_resolution=explicit" in out
    assert f"editor_decision_source_identity_key={GLAD}" in out
    assert REJECT_NOTE in out


# --- Review of #917 ---


def _write(state_dir: Path, name: str, value: Any) -> None:
    (state_dir / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_a_single_snapshot_row_shows_the_chosen_decision(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)
    rejected_row = {
        "candidate_id": "candidate-own-time",
        "identity_key": OWN_TIME,
        "title": "IN MY OWN TIME",
        "primary_authors": ["Christopher Lloyd"],
        "update_delta": "unchanged",
        "editor_status": "reject",
        "editor_note": REJECT_NOTE,
    }
    seed_state(state_dir, latest_scouting_candidates=[rejected_row])

    apply_identity_correction(state_dir, _correction(ALL))

    (row,) = read_state(state_dir, "latest_scouting_candidates")
    assert row["identity_key"] == GLAD
    assert row["editor_status"] == "needs_deeper_analysis" and row["editor_note"] == DEEPER_NOTE


def test_a_second_merge_keeps_the_decisions_an_earlier_merge_set_aside(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)
    apply_identity_correction(state_dir, _correction([OWN_TIME, GLAD]))
    history = read_state(state_dir, "candidate_history")
    history.append(
        {
            "identity_key": "glad to be here|christopher lloyd",
            "title": "Glad To Be Here",
            "primary_authors": ["Christopher Lloyd"],
            "first_seen_date": "2026-11-02",
            "editor_status": "maybe_watch",
            "editor_note": "Посмотреть позже.",
        }
    )
    seed_state(state_dir, candidate_history=history)
    merged_glad = next(record for record in history if record["identity_key"] == GLAD)
    second = {
        "source_identity_keys": [GLAD, "glad to be here|christopher lloyd"],
        "canonical": {"identity_key": GLAD, "title": "Glad I'm Still Alive", "primary_authors": ["Christopher Lloyd"]},
        "editor_decision_resolution": {
            "canonical_source_identity_key": GLAD,
            "observed_editor_decisions": {
                GLAD: {"editor_status": merged_glad["editor_status"], "editor_note": merged_glad["editor_note"]},
                "glad to be here|christopher lloyd": {
                    "editor_status": "maybe_watch",
                    "editor_note": "Посмотреть позже.",
                },
            },
        },
    }

    apply_identity_correction(state_dir, second)

    merged = next(record for record in read_state(state_dir, "candidate_history") if record["identity_key"] == GLAD)
    kept = merged[EDITOR_DECISION_RESOLUTION_RECORD_FIELD]["discarded_decisions"]
    assert {entry["source_identity_key"] for entry in kept} == {OWN_TIME, "glad to be here|christopher lloyd"}
    assert any(entry.get("editor_note") == REJECT_NOTE for entry in kept)


def test_a_new_row_merged_with_an_old_row_in_one_snapshot_is_not_new(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)
    old_row = {"candidate_id": "candidate-glad", "identity_key": GLAD, "update_delta": "unchanged"}
    new_row = {**_today_row(), "publisher": "Grand Central", "isbn": ""}
    seed_state(state_dir, latest_scouting_candidates=[old_row, new_row])

    apply_identity_correction(state_dir, _correction([GLAD, IM_GLAD], resolve=False))

    (row,) = read_state(state_dir, "latest_scouting_candidates")
    assert row["identity_key"] == GLAD
    assert row["update_delta"] != "new"


def test_pending_feedback_for_a_merged_identity_refuses_a_decision_resolution(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)
    seed_state(
        state_dir,
        pending_editor_rows=[{"identity_key": OWN_TIME, "Editor Status": "reject", "Editor Note": "Ещё раз нет."}],
    )
    before = _snapshot(state_dir)
    with pytest.raises(ValueError, match="editor feedback is pending for in my own time"):
        apply_identity_correction(state_dir, _correction(ALL))
    assert _snapshot(state_dir) == before


def _with_memo_queue(state_dir: Path) -> None:
    history = _history()
    history[1]["memo_queue_marker"] = "needs_deeper_analysis"
    seed_state(state_dir, candidate_history=history)
    _write(
        state_dir,
        "memo-queue.json",
        [
            {
                "identity_key": GLAD,
                "title": "Glad I'm Still Alive",
                "editor_note": DEEPER_NOTE,
                "status": "queued_for_future_memo",
            }
        ],
    )


def test_choosing_reject_takes_the_work_out_of_the_memo_queue(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)
    _with_memo_queue(state_dir)

    apply_identity_correction(state_dir, _correction(ALL, chosen=OWN_TIME))

    (merged,) = read_state(state_dir, "candidate_history")
    assert merged["editor_status"] == "reject" and merged["memo_queue_marker"] == ""
    assert json.loads((state_dir / "memo-queue.json").read_text(encoding="utf-8")) == []


def test_choosing_deeper_analysis_keeps_one_queue_entry_with_the_chosen_note(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)
    _with_memo_queue(state_dir)

    apply_identity_correction(state_dir, _correction(ALL, chosen=GLAD))

    (merged,) = read_state(state_dir, "candidate_history")
    assert merged["memo_queue_marker"] == "needs_deeper_analysis"
    (entry,) = json.loads((state_dir / "memo-queue.json").read_text(encoding="utf-8"))
    assert entry["identity_key"] == GLAD and entry["editor_note"] == DEEPER_NOTE


def test_pending_feedback_under_an_older_alias_refuses_a_decision_resolution(tmp_path: Path) -> None:
    # run-mvp resolves an alias to the merged durable ID, so its feedback would
    # overwrite the chosen decision (review of #917).
    from autoscout.candidate_identity import empty_alias_table

    state_dir = _state(tmp_path)
    old_alias = "in my own time a memoir|christopher lloyd"
    table = empty_alias_table()
    table["aliases"] = {old_alias: "candidate-own-time", OWN_TIME: "candidate-own-time"}
    seed_state(
        state_dir,
        candidate_identity_aliases=table,
        pending_editor_rows=[{"identity_key": old_alias, "Editor Status": "reject", "Editor Note": "Ещё раз нет."}],
    )
    before = _snapshot(state_dir)
    with pytest.raises(ValueError, match=f"editor feedback is pending for {old_alias}"):
        apply_identity_correction(state_dir, _correction(ALL))
    assert _snapshot(state_dir) == before


def test_choosing_a_status_that_does_not_suppress_drops_the_suppression_entry(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)
    entry = {
        "identity_key": OWN_TIME,
        "title": "IN MY OWN TIME",
        "editor_status": "reject",
        "suppression_reason": "suppressed by editor status without Material Change",
    }
    _write(state_dir, "suppressed-candidates.json", [entry])

    apply_identity_correction(state_dir, _correction(ALL))

    assert json.loads((state_dir / "suppressed-candidates.json").read_text(encoding="utf-8")) == []


def test_choosing_reject_keeps_the_suppression_entry(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)
    entry = {
        "identity_key": OWN_TIME,
        "title": "IN MY OWN TIME",
        "editor_status": "reject",
        "suppression_reason": "suppressed by editor status without Material Change",
    }
    _write(state_dir, "suppressed-candidates.json", [entry])

    apply_identity_correction(state_dir, _correction(ALL, chosen=OWN_TIME))

    (kept,) = json.loads((state_dir / "suppressed-candidates.json").read_text(encoding="utf-8"))
    assert kept["identity_key"] == GLAD and kept["editor_status"] == "reject"


def test_a_chosen_decision_without_a_status_cannot_set_aside_one_with_a_status(tmp_path: Path) -> None:
    # The review would fall back to the discarded CSV status (review of #917).
    state_dir = _state(tmp_path)
    history = _history()
    del history[1]["editor_status"]
    seed_state(state_dir, candidate_history=history)
    before = _snapshot(state_dir)
    observed = {**_observed(), GLAD: {"editor_note": DEEPER_NOTE}}
    with pytest.raises(ValueError, match="without editor_status over one that has it"):
        apply_identity_correction(state_dir, _correction(ALL, observed=observed))
    assert _snapshot(state_dir) == before
