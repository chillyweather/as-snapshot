"""The learned-field envelope's merge hook, without any completion pass (issue #968)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from state_store_support import seed_state

from autoscout.learned_field_completion import LearnedFieldSpec
from autoscout.learned_field_completion import merge_into_fresh_history
from autoscout.state_store import StateStore
from autoscout.state_store import open_state_store


def _spec(**extra: Any) -> LearnedFieldSpec:
    return LearnedFieldSpec(
        fields=("a", "b"),
        already_filled=lambda record: bool(record.get("filled")),
        copy_to_row=lambda record, row: False,
        report_filename="report.json",
        seal_pending_filename="pending.json",
        label="envelope test",
        project=lambda history, rows, aliases: (list(rows), 0),
        seal=lambda output_dir, store: None,
        **extra,
    )


def _history(tmp_path: Path, records: list[dict[str, Any]]) -> StateStore:
    seed_state(tmp_path, candidate_history=records)
    return open_state_store(tmp_path)


def test_without_a_hook_the_generic_loop_copies_present_fields_and_pops_absent_ones(tmp_path):
    path = _history(tmp_path, [{"identity_key": "k", "a": "old", "b": "fresh-only"}])

    merged = merge_into_fresh_history(path, [{"identity_key": "k", "a": "new"}], _spec())

    assert merged == [{"identity_key": "k", "a": "new"}]


def test_a_hook_replaces_the_generic_loop_and_receives_fresh_and_source_records(tmp_path):
    path = _history(tmp_path, [{"identity_key": "k", "a": "old", "b": "fresh-only"}])
    seen: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def hook(fresh: dict[str, Any], source: Any) -> None:
        seen.append((dict(fresh), dict(source)))
        fresh["c"] = source["a"]

    merged = merge_into_fresh_history(path, [{"identity_key": "k", "a": "new"}], _spec(merge_record=hook))

    # The generic loop would have overwritten "a" and removed "b"; the hook decided instead.
    assert merged == [{"identity_key": "k", "a": "old", "b": "fresh-only", "c": "new"}]
    assert seen == [({"identity_key": "k", "a": "old", "b": "fresh-only"}, {"identity_key": "k", "a": "new"})]


def test_a_record_that_already_holds_its_value_is_skipped_before_the_hook(tmp_path):
    path = _history(tmp_path, [{"identity_key": "k", "a": "kept", "filled": True}])
    calls: list[str] = []

    merged = merge_into_fresh_history(
        path,
        [{"identity_key": "k", "a": "new"}],
        _spec(merge_record=lambda fresh, source: calls.append("called")),
    )

    assert calls == []
    assert merged == [{"identity_key": "k", "a": "kept", "filled": True}]
