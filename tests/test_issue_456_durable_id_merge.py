"""Approved durable Candidate ID merges (Issue #456).

Two history records that already carry different durable IDs cannot be merged
by the default migration: the append-only alias table reports
``multiple_durable_ids`` and apply stays blocked. This suite covers the
explicitly approved path that reuses the same migration and persistence code,
records a redirect instead of rewriting stored aliases, and preserves every
editor decision verbatim.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from state_store_support import fail_database_commits
from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout.__main__ import main
from autoscout.candidate_history_migration import build_candidate_history_migration_plan
from autoscout.candidate_history_migration import build_durable_id_merge_plan
from autoscout.candidate_history_migration import emit_durable_merge_approval
from autoscout.candidate_history_migration import load_durable_merge_approvals
from autoscout.candidate_history_migration import migrate_candidate_history
from autoscout.candidate_history_migration import validate_durable_merge_approval
from autoscout.candidate_identity import assign_alias
from autoscout.candidate_identity import effective_candidate_id
from autoscout.candidate_identity import resolve_candidate_id
from autoscout.candidate_identity import stored_candidate_id_for_alias
from autoscout.candidate_identity import validate_alias_table

REJECT_NOTE = (
    "Бестселлер про менопаузу. Книгу либо не продают в РФ, "
    "либо тема женского здоровья в таком ключе у нас плохо продаётся."
)
RETIRED_ID = "candidate-ad10f4378f94afbe07d71f3a"
SURVIVOR_ID = "candidate-cad6745f4e85571d41274186"
CANONICAL_KEY = "woman on fire|sheila de liz"


def _evidence(source: str) -> dict[str, str]:
    return {
        "source_family": "bestseller/ranking",
        "source_name": source,
        "evidence_url": "https://www.amazon.de/dp/B081SJK2C9",
    }


def woman_on_fire_history() -> list[dict[str, object]]:
    return [
        {
            "identity_key": "woman on fire|med sheila de liz",
            "title": "Woman on Fire",
            "primary_authors": ["Dr. med. Sheila de Liz"],
            "candidate_id": RETIRED_ID,
            "first_seen_date": "2026-06-10",
            "last_seen_date": "2026-06-10",
            "independent_signal_count": 1,
            "independent_signal_families": ["bestseller/ranking"],
            "source_evidence": [_evidence("Amazon.de Charts")],
        },
        {
            "identity_key": CANONICAL_KEY,
            "title": "Woman on Fire",
            "primary_authors": ["Sheila de Liz"],
            "candidate_id": SURVIVOR_ID,
            "editor_status": "reject",
            "editor_note": REJECT_NOTE,
            "first_seen_date": "2026-08-13",
            "last_seen_date": "2026-09-03",
            "independent_signal_count": 1,
            "independent_signal_families": ["bestseller/ranking"],
            "source_evidence": [_evidence("Amazon.de Charts")],
        },
    ]


def woman_on_fire_aliases() -> dict[str, object]:
    return {
        "version": 1,
        "aliases": {
            "woman on fire|med sheila de liz": RETIRED_ID,
            CANONICAL_KEY: SURVIVOR_ID,
        },
        "candidates": {
            RETIRED_ID: {"candidate_id": RETIRED_ID},
            SURVIVOR_ID: {"candidate_id": SURVIVOR_ID},
        },
    }


def _expected_members(history: list[dict[str, object]]) -> list[dict[str, object]]:
    members: list[dict[str, object]] = []
    for record in history:
        authors = record.get("primary_authors", [])
        members.append(
            {
                "candidate_id": str(record.get("candidate_id", "")),
                "identity_key": str(record.get("identity_key", "")),
                "title": str(record.get("title", "")),
                "primary_authors": list(authors) if isinstance(authors, list) else [],
                "editor_status": str(record.get("editor_status") or ""),
                "editor_note": str(record.get("editor_note") or ""),
            }
        )
    return members


def woman_on_fire_approval(history: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "canonical_identity_key": CANONICAL_KEY,
        "survivor_candidate_id": SURVIVOR_ID,
        "retired_candidate_ids": [RETIRED_ID],
        "expected_members": _expected_members(history or woman_on_fire_history()),
    }


def _write_state(state: Path, history: list[dict[str, object]], aliases: dict[str, object]) -> None:
    state.mkdir(parents=True, exist_ok=True)
    seed_state(state, candidate_history=history)
    seed_state(state, candidate_identity_aliases=aliases)


def test_blocked_preview_without_approval_reports_multiple_durable_ids(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write_state(state, woman_on_fire_history(), woman_on_fire_aliases())
    history_before = state_snapshot(state)
    aliases_before = state_snapshot(state)

    report, changed = migrate_candidate_history(state)

    assert changed is False
    assert report["merge_group_count"] == 1
    assert report["projected_history_count"] == 1
    assert report["apply_blocked"] is True
    assert report["alias_conflict_count"] == 1
    conflict = report["alias_conflicts"][0]
    assert conflict["canonical_identity_key"] == CANONICAL_KEY
    assert sorted(conflict["candidate_ids"]) == sorted([RETIRED_ID, SURVIVOR_ID])
    assert conflict["reason"] == "multiple_durable_ids"
    with pytest.raises(ValueError, match="blocked"):
        migrate_candidate_history(state, apply=True)
    assert state_snapshot(state) == history_before
    assert state_snapshot(state) == aliases_before


def test_durable_plan_shows_survivor_decisions_and_guard() -> None:
    plan = build_durable_id_merge_plan(woman_on_fire_history(), woman_on_fire_aliases(), woman_on_fire_approval())

    assert plan["status"] == "ready"
    assert plan["survivor_candidate_id"] == SURVIVOR_ID
    assert plan["retired_candidate_ids"] == [RETIRED_ID]
    assert plan["survivor_rationale"] == "survivor carries the only editor decision"
    assert plan["guard"]["passed"] is True
    assert plan["guard"]["normalized_title"] == "woman on fire"
    assert "sheila de liz" in plan["guard"]["shared_authors"]
    assert plan["editor_decisions"] == [{"status": "reject", "note": REJECT_NOTE}]
    assert plan["affected_candidate_ids"] == sorted([RETIRED_ID, SURVIVOR_ID])
    assert "woman on fire|med sheila de liz" in plan["affected_identity_keys"]
    assert CANONICAL_KEY in plan["affected_identity_keys"]
    stored = {item["alias"]: item["stored_candidate_id"] for item in plan["alias_effects"]}
    assert stored["woman on fire|med sheila de liz"] == RETIRED_ID
    assert stored[CANONICAL_KEY] == SURVIVOR_ID


def test_approved_preview_unblocks_with_zero_mutation(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write_state(state, woman_on_fire_history(), woman_on_fire_aliases())
    history_before = state_snapshot(state)
    aliases_before = state_snapshot(state)

    report, changed = migrate_candidate_history(state, durable_merge_approvals=[woman_on_fire_approval()])

    assert changed is False
    assert report["apply_blocked"] is False
    assert report["alias_conflict_count"] == 0
    assert report["durable_merge_count"] == 1
    merge = report["durable_merges"][0]
    assert merge["status"] == "ready"
    assert merge["survivor_candidate_id"] == SURVIVOR_ID
    assert merge["affected_candidate_ids"] == sorted([RETIRED_ID, SURVIVOR_ID])
    assert merge["editor_decisions"] == [{"status": "reject", "note": REJECT_NOTE}]
    projected = report["projected_history"][0]
    assert projected["candidate_id"] == SURVIVOR_ID
    assert projected["editor_status"] == "reject"
    assert projected["editor_note"] == REJECT_NOTE
    assert state_snapshot(state) == history_before
    assert state_snapshot(state) == aliases_before


def test_apply_preserves_reject_note_signals_evidence_and_dates(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write_state(state, woman_on_fire_history(), woman_on_fire_aliases())

    report, changed = migrate_candidate_history(state, apply=True, durable_merge_approvals=[woman_on_fire_approval()])

    assert changed is True
    assert report["apply_blocked"] is False
    history = read_state(state, "candidate_history")
    assert len(history) == 1
    merged = history[0]
    assert merged["identity_key"] == CANONICAL_KEY
    assert merged["candidate_id"] == SURVIVOR_ID
    assert merged["editor_status"] == "reject"
    assert merged["editor_note"] == REJECT_NOTE
    assert merged["first_seen_date"] == "2026-06-10"
    assert merged["last_seen_date"] == "2026-09-03"
    assert set(merged["independent_signal_families"]) == {"bestseller/ranking"}
    assert merged["independent_signal_count"] >= 1
    assert len(merged["source_evidence"]) == 2


def test_apply_unions_distinct_signal_families_and_evidence(tmp_path: Path) -> None:
    history = woman_on_fire_history()
    history[0]["independent_signal_families"] = ["award"]
    history[0]["independent_signal_count"] = 2
    history[0]["source_evidence"] = [
        {"source_family": "award", "source_name": "Award", "evidence_url": "https://example.test/award"}
    ]
    state = tmp_path / "state"
    _write_state(state, history, woman_on_fire_aliases())

    _report, changed = migrate_candidate_history(state, apply=True, durable_merge_approvals=[woman_on_fire_approval()])

    assert changed is True
    merged = read_state(state, "candidate_history")[0]
    assert set(merged["independent_signal_families"]) == {"award", "bestseller/ranking"}
    assert merged["independent_signal_count"] >= 2
    assert len(merged["source_evidence"]) == 2


def test_retired_aliases_and_explicit_id_resolve_to_survivor(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write_state(state, woman_on_fire_history(), woman_on_fire_aliases())
    migrate_candidate_history(state, apply=True, durable_merge_approvals=[woman_on_fire_approval()])

    table = validate_alias_table(read_state(state, "candidate_identity_aliases"))
    assert table["version"] == 2
    assert stored_candidate_id_for_alias(table, "woman on fire|med sheila de liz") == RETIRED_ID
    assert stored_candidate_id_for_alias(table, CANONICAL_KEY) == SURVIVOR_ID
    assert resolve_candidate_id({"identity_key": "woman on fire|med sheila de liz"}, table) == SURVIVOR_ID
    assert resolve_candidate_id({"identity_key": CANONICAL_KEY}, table) == SURVIVOR_ID
    assert (
        resolve_candidate_id({"identity_key": "unseen|key", "title": "Unseen", "candidate_id": RETIRED_ID}, table)
        == SURVIVOR_ID
    )
    assert effective_candidate_id(RETIRED_ID, table) == SURVIVOR_ID
    assert effective_candidate_id(SURVIVOR_ID, table) == SURVIVOR_ID


def test_audit_keeps_retired_identity_and_merge_record(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write_state(state, woman_on_fire_history(), woman_on_fire_aliases())
    migrate_candidate_history(state, apply=True, durable_merge_approvals=[woman_on_fire_approval()])

    table = read_state(state, "candidate_identity_aliases")
    assert table["redirects"] == {RETIRED_ID: SURVIVOR_ID}
    assert len(table["durable_merges"]) == 1
    audit = table["durable_merges"][0]
    assert audit["canonical_identity_key"] == CANONICAL_KEY
    assert audit["survivor_candidate_id"] == SURVIVOR_ID
    assert audit["retired_candidate_ids"] == [RETIRED_ID]
    retired_meta = table["candidates"][RETIRED_ID]
    assert retired_meta["redirect_to"] == SURVIVOR_ID
    assert table["aliases"]["woman on fire|med sheila de liz"] == RETIRED_ID


def test_guard_refuses_different_authors_for_same_title() -> None:
    history = [
        {
            "identity_key": "elon musk|ashlee vance",
            "title": "Elon Musk",
            "primary_authors": ["Ashlee Vance"],
            "candidate_id": "candidate-vance",
        },
        {
            "identity_key": "elon musk|walter isaacson",
            "title": "Elon Musk",
            "primary_authors": ["Walter Isaacson"],
            "candidate_id": "candidate-isaacson",
        },
    ]
    aliases = {
        "version": 1,
        "aliases": {"elon musk|ashlee vance": "candidate-vance", "elon musk|walter isaacson": "candidate-isaacson"},
        "candidates": {"candidate-vance": {"candidate_id": "candidate-vance"}},
    }
    approval = {
        "canonical_identity_key": "elon musk|ashlee vance",
        "survivor_candidate_id": "candidate-vance",
        "retired_candidate_ids": ["candidate-isaacson"],
        "expected_members": [
            {
                "candidate_id": "candidate-vance",
                "identity_key": "elon musk|ashlee vance",
                "title": "Elon Musk",
                "primary_authors": ["Ashlee Vance"],
                "editor_status": "",
                "editor_note": "",
            },
            {
                "candidate_id": "candidate-isaacson",
                "identity_key": "elon musk|walter isaacson",
                "title": "Elon Musk",
                "primary_authors": ["Walter Isaacson"],
                "editor_status": "",
                "editor_note": "",
            },
        ],
    }

    plan = build_durable_id_merge_plan(history, aliases, approval)

    assert plan["status"] == "invalid"
    assert plan["reason"] == "identity guard failed"
    assert plan["guard"]["passed"] is False
    full = build_candidate_history_migration_plan(history, None, aliases, [approval])
    assert full["durable_merges"][0]["status"] == "invalid"
    assert full["apply_blocked"] is True


def test_conflicting_editor_decisions_are_preserved_and_reported(tmp_path: Path) -> None:
    history = [
        {
            "identity_key": "conflict|same",
            "title": "Conflict Work",
            "primary_authors": ["Same Author"],
            "candidate_id": "candidate-one",
            "editor_status": "reject",
            "editor_note": "First decision.",
        },
        {
            "identity_key": "conflict|same author",
            "title": "Conflict Work",
            "primary_authors": ["Same Author"],
            "candidate_id": "candidate-two",
            "editor_status": "interested",
            "editor_note": "Second decision.",
        },
    ]
    aliases = {
        "version": 1,
        "aliases": {"conflict|same": "candidate-one", "conflict|same author": "candidate-two"},
        "candidates": {"candidate-one": {"candidate_id": "candidate-one"}},
    }
    approval = {
        "canonical_identity_key": "conflict|same author",
        "survivor_candidate_id": "candidate-one",
        "retired_candidate_ids": ["candidate-two"],
        "expected_members": _expected_members(history),
    }
    state = tmp_path / "state"
    _write_state(state, history, aliases)
    history_before = state_snapshot(state)
    aliases_before = state_snapshot(state)

    plan = build_durable_id_merge_plan(history, aliases, approval)

    assert plan["status"] == "blocked"
    assert plan["editor_status_conflicts"] == ["reject", "interested"]
    assert len(plan["editor_decisions"]) == 2
    report, changed = migrate_candidate_history(state, durable_merge_approvals=[approval])
    assert changed is False
    assert report["apply_blocked"] is True
    assert report["durable_merges"][0]["status"] == "blocked"
    with pytest.raises(ValueError, match="blocked"):
        migrate_candidate_history(state, apply=True, durable_merge_approvals=[approval])
    assert state_snapshot(state) == history_before
    assert state_snapshot(state) == aliases_before
    current = read_state(state, "candidate_history")
    assert {record["editor_status"] for record in current} == {"reject", "interested"}


def test_note_without_status_blocks_merge(tmp_path: Path) -> None:
    history = [
        {
            "identity_key": "note|same",
            "title": "Note Work",
            "primary_authors": ["Same Author"],
            "candidate_id": "candidate-one",
            "editor_note": "Needs a decision.",
        },
        {
            "identity_key": "note|same author",
            "title": "Note Work",
            "primary_authors": ["Same Author"],
            "candidate_id": "candidate-two",
        },
    ]
    aliases = {
        "version": 1,
        "aliases": {"note|same": "candidate-one", "note|same author": "candidate-two"},
        "candidates": {},
    }
    approval = {
        "canonical_identity_key": "note|same author",
        "survivor_candidate_id": "candidate-one",
        "retired_candidate_ids": ["candidate-two"],
        "expected_members": _expected_members(history),
    }
    state = tmp_path / "state"
    _write_state(state, history, aliases)

    plan = build_durable_id_merge_plan(history, aliases, approval)

    assert plan["status"] == "blocked"
    report, changed = migrate_candidate_history(state, durable_merge_approvals=[approval])
    assert changed is False
    assert report["apply_blocked"] is True


def test_wrong_survivor_is_invalid_with_zero_mutation(tmp_path: Path) -> None:
    approval = {
        "canonical_identity_key": CANONICAL_KEY,
        "survivor_candidate_id": RETIRED_ID,
        "retired_candidate_ids": [SURVIVOR_ID],
        "expected_members": _expected_members(woman_on_fire_history()),
    }
    state = tmp_path / "state"
    _write_state(state, woman_on_fire_history(), woman_on_fire_aliases())
    history_before = state_snapshot(state)
    aliases_before = state_snapshot(state)

    plan = build_durable_id_merge_plan(woman_on_fire_history(), woman_on_fire_aliases(), approval)

    assert plan["status"] == "invalid"
    assert plan["reason"] == "approval names the wrong survivor"
    report, changed = migrate_candidate_history(state, durable_merge_approvals=[approval])
    assert changed is False
    assert report["apply_blocked"] is True
    assert report["durable_merges"][0]["status"] == "invalid"
    assert state_snapshot(state) == history_before
    assert state_snapshot(state) == aliases_before


def test_stale_approval_is_refused_with_zero_mutation(tmp_path: Path) -> None:
    approval = {
        "canonical_identity_key": "missing|key",
        "survivor_candidate_id": "candidate-ghost-a",
        "retired_candidate_ids": ["candidate-ghost-b"],
        "expected_members": [
            {
                "candidate_id": "candidate-ghost-a",
                "identity_key": "missing|key",
                "title": "Missing",
                "primary_authors": ["Ghost"],
                "editor_status": "",
                "editor_note": "",
            },
            {
                "candidate_id": "candidate-ghost-b",
                "identity_key": "missing|key alt",
                "title": "Missing",
                "primary_authors": ["Ghost"],
                "editor_status": "",
                "editor_note": "",
            },
        ],
    }
    state = tmp_path / "state"
    _write_state(state, woman_on_fire_history(), woman_on_fire_aliases())
    history_before = state_snapshot(state)
    aliases_before = state_snapshot(state)

    plan = build_durable_id_merge_plan(woman_on_fire_history(), woman_on_fire_aliases(), approval)

    assert plan["status"] == "stale"
    report, changed = migrate_candidate_history(state, durable_merge_approvals=[approval])
    assert changed is False
    assert report["apply_blocked"] is True
    assert report["durable_merges"][0]["status"] == "stale"
    assert state_snapshot(state) == history_before
    assert state_snapshot(state) == aliases_before


def test_malformed_approvals_are_rejected() -> None:
    with pytest.raises(ValueError, match="canonical identity key"):
        validate_durable_merge_approval({"survivor_candidate_id": "a", "retired_candidate_ids": ["b"]})
    with pytest.raises(ValueError, match="survivor"):
        validate_durable_merge_approval({"canonical_identity_key": "k", "retired_candidate_ids": ["b"]})
    with pytest.raises(ValueError, match="retired"):
        validate_durable_merge_approval({"canonical_identity_key": "k", "survivor_candidate_id": "a"})
    with pytest.raises(ValueError, match="must not be retired"):
        validate_durable_merge_approval(
            {"canonical_identity_key": "k", "survivor_candidate_id": "a", "retired_candidate_ids": ["a"]}
        )
    with pytest.raises(ValueError, match="pairwise"):
        validate_durable_merge_approval(
            {"canonical_identity_key": "k", "survivor_candidate_id": "a", "retired_candidate_ids": ["b", "c"]}
        )
    with pytest.raises(ValueError, match="expected members"):
        validate_durable_merge_approval(
            {"canonical_identity_key": "k", "survivor_candidate_id": "a", "retired_candidate_ids": ["b"]}
        )
    with pytest.raises(ValueError, match="exactly the approved IDs"):
        validate_durable_merge_approval(
            {
                "canonical_identity_key": "k",
                "survivor_candidate_id": "a",
                "retired_candidate_ids": ["b"],
                "expected_members": [
                    {
                        "candidate_id": "a",
                        "identity_key": "k",
                        "title": "T",
                        "primary_authors": ["A"],
                        "editor_status": "",
                        "editor_note": "",
                    }
                ],
            }
        )


def test_approval_file_loading_rejects_bad_input(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text(json.dumps(woman_on_fire_approval()), encoding="utf-8")
    assert load_durable_merge_approvals([good]) == [woman_on_fire_approval()]
    assert load_durable_merge_approvals(None) == []
    assert load_durable_merge_approvals([]) == []
    missing = tmp_path / "missing.json"
    with pytest.raises(ValueError, match="not found"):
        load_durable_merge_approvals([missing])
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid durable merge approval JSON"):
        load_durable_merge_approvals([broken])
    shape = tmp_path / "shape.json"
    shape.write_text(json.dumps({"canonical_identity_key": "k"}), encoding="utf-8")
    with pytest.raises(ValueError, match="survivor"):
        load_durable_merge_approvals([shape])


def test_repeated_application_is_idempotent(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write_state(state, woman_on_fire_history(), woman_on_fire_aliases())

    _first_report, first_changed = migrate_candidate_history(
        state, apply=True, durable_merge_approvals=[woman_on_fire_approval()]
    )
    assert first_changed is True
    history_bytes = state_snapshot(state)
    aliases_bytes = state_snapshot(state)

    second_report, second_changed = migrate_candidate_history(
        state, apply=True, durable_merge_approvals=[woman_on_fire_approval()]
    )
    assert second_changed is False
    assert second_report["durable_merges"][0]["status"] == "already_applied"
    assert state_snapshot(state) == history_bytes
    assert state_snapshot(state) == aliases_bytes

    third_report, third_changed = migrate_candidate_history(
        state, apply=True, durable_merge_approvals=[woman_on_fire_approval()]
    )
    assert third_changed is False
    assert third_report["durable_merges"][0]["status"] == "already_applied"
    table = read_state(state, "candidate_identity_aliases")
    assert len(table["durable_merges"]) == 1


def test_merged_history_without_recorded_merge_is_refused(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write_state(state, woman_on_fire_history(), woman_on_fire_aliases())
    migrate_candidate_history(state, apply=True, durable_merge_approvals=[woman_on_fire_approval()])
    merged_history = read_state(state, "candidate_history")
    _write_state(state, merged_history, woman_on_fire_aliases())
    history_before = state_snapshot(state)
    aliases_before = state_snapshot(state)

    report, changed = migrate_candidate_history(state, durable_merge_approvals=[woman_on_fire_approval()])

    assert changed is False
    assert report["apply_blocked"] is True
    assert report["durable_merges"][0]["status"] == "stale"
    with pytest.raises(ValueError, match="blocked"):
        migrate_candidate_history(state, apply=True, durable_merge_approvals=[woman_on_fire_approval()])
    assert state_snapshot(state) == history_before
    assert state_snapshot(state) == aliases_before


def test_interrupted_apply_leaves_verifiable_state_and_retry_completes(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write_state(state, woman_on_fire_history(), woman_on_fire_aliases())

    report, changed = migrate_candidate_history(state, apply=True, durable_merge_approvals=[woman_on_fire_approval()])

    assert changed is True
    assert report["apply_blocked"] is False
    table = read_state(state, "candidate_identity_aliases")
    assert table["redirects"] == {RETIRED_ID: SURVIVOR_ID}
    assert len(table["durable_merges"]) == 1


def test_write_failure_propagates_and_retry_converges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from autoscout import safe_artifacts
    from autoscout.state_store import json_backend

    state = tmp_path / "state"
    _write_state(state, woman_on_fire_history(), woman_on_fire_aliases())
    before = state_snapshot(state)
    real_publish_bundle = json_backend.publish_bundle
    calls = {"count": 0}

    def flaky_replace(source: Path, destination: Path) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("simulated history write failure")
        safe_artifacts.staged_replace(source, destination)

    def publish_bundle(paths, contents, **_kwargs):  # type: ignore[no-untyped-def]
        return real_publish_bundle(paths, contents, replace=flaky_replace)

    monkeypatch.setattr(json_backend, "publish_bundle", publish_bundle)
    fail_database_commits(monkeypatch, message="simulated history write failure")
    with pytest.raises(OSError, match="simulated history write failure"):
        migrate_candidate_history(state, apply=True, durable_merge_approvals=[woman_on_fire_approval()])
    monkeypatch.undo()
    # The alias table and history are one State Store transaction (#1070):
    # the failure leaves both as they were, and the retry completes.
    assert state_snapshot(state) == before

    report, changed = migrate_candidate_history(state, apply=True, durable_merge_approvals=[woman_on_fire_approval()])
    assert changed is True
    assert report["apply_blocked"] is False
    history = read_state(state, "candidate_history")
    table = read_state(state, "candidate_identity_aliases")
    assert len(history) == 1
    assert history[0]["candidate_id"] == SURVIVOR_ID
    assert table["redirects"] == {RETIRED_ID: SURVIVOR_ID}


def test_unrelated_conflict_still_blocks_with_one_approval(tmp_path: Path) -> None:
    history = [
        *woman_on_fire_history(),
        {
            "identity_key": "other|alice",
            "title": "Other",
            "primary_authors": ["Alice"],
            "candidate_id": "candidate-other-a",
        },
        {
            "identity_key": "other|alice alt",
            "title": "Other",
            "primary_authors": ["Alice"],
            "candidate_id": "candidate-other-b",
        },
    ]
    aliases = woman_on_fire_aliases()
    assert isinstance(aliases["aliases"], dict)
    aliases["aliases"]["other|alice"] = "candidate-other-a"
    aliases["aliases"]["other|alice alt"] = "candidate-other-b"
    state = tmp_path / "state"
    _write_state(state, history, aliases)

    report, changed = migrate_candidate_history(state, durable_merge_approvals=[woman_on_fire_approval()])

    assert changed is False
    assert report["apply_blocked"] is True
    assert report["alias_conflict_count"] == 1
    assert report["durable_merges"][0]["status"] == "ready"


def test_cli_dry_run_then_apply_with_approval_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = tmp_path / "state"
    _write_state(state, woman_on_fire_history(), woman_on_fire_aliases())
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(json.dumps(woman_on_fire_approval()), encoding="utf-8")
    history_before = state_snapshot(state)

    dry_exit = main(
        ["migrate-candidate-history", "--state-dir", str(state), "--durable-merge-approval", str(approval_path)]
    )
    dry_out = capsys.readouterr().out
    assert dry_exit == 0
    assert "candidate_history_migration=dry-run" in dry_out
    assert "apply_blocked=false" in dry_out
    assert f"survivor={SURVIVOR_ID}" in dry_out
    assert state_snapshot(state) == history_before

    apply_exit = main(
        [
            "migrate-candidate-history",
            "--state-dir",
            str(state),
            "--apply",
            "--durable-merge-approval",
            str(approval_path),
        ]
    )
    apply_out = capsys.readouterr().out
    assert apply_exit == 0
    assert "candidate_history_migration=applied" in apply_out
    assert "candidate_history_changed=true" in apply_out
    merged = read_state(state, "candidate_history")
    assert len(merged) == 1
    assert merged[0]["editor_note"] == REJECT_NOTE


def test_cli_rejects_missing_approval_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = tmp_path / "state"
    _write_state(state, woman_on_fire_history(), woman_on_fire_aliases())

    exit_code = main(
        ["migrate-candidate-history", "--state-dir", str(state), "--durable-merge-approval", str(tmp_path / "no.json")]
    )

    assert exit_code == 1
    assert "error=candidate_history_migration" in capsys.readouterr().out


def test_versioned_tables_validate_and_reject_cycles() -> None:
    assert validate_alias_table(None)["version"] == 1
    assert validate_alias_table({"version": 1, "aliases": {}, "candidates": {}})["version"] == 1
    merged_table = {
        "version": 2,
        "aliases": {"a|x": "candidate-a", "a|y": "candidate-b"},
        "candidates": {"candidate-a": {"candidate_id": "candidate-a"}},
        "redirects": {"candidate-a": "candidate-b"},
        "durable_merges": [
            {
                "canonical_identity_key": "a|y",
                "survivor_candidate_id": "candidate-b",
                "retired_candidate_ids": ["candidate-a"],
            }
        ],
    }
    assert validate_alias_table(merged_table)["version"] == 2
    with pytest.raises(ValueError, match="unsupported"):
        validate_alias_table({"version": 99, "aliases": {}, "candidates": {}})
    with pytest.raises(ValueError, match="version 2"):
        validate_alias_table({"version": 1, "aliases": {}, "candidates": {}, "redirects": {}})
    with pytest.raises(ValueError, match="cycle"):
        validate_alias_table(
            {
                "version": 2,
                "aliases": {},
                "candidates": {},
                "redirects": {"candidate-a": "candidate-b", "candidate-b": "candidate-a"},
                "durable_merges": [],
            }
        )


def test_cross_work_approval_without_retired_history_is_refused(tmp_path: Path) -> None:
    history = [
        {
            "identity_key": "elon musk|ashlee vance",
            "title": "Elon Musk",
            "primary_authors": ["Ashlee Vance"],
            "candidate_id": "candidate-elon-vance",
        },
        {
            "identity_key": CANONICAL_KEY,
            "title": "Woman on Fire",
            "primary_authors": ["Sheila de Liz"],
            "candidate_id": SURVIVOR_ID,
            "editor_status": "reject",
            "editor_note": REJECT_NOTE,
        },
    ]
    aliases = {
        "version": 1,
        "aliases": {
            "elon musk|ashlee vance": "candidate-elon-vance",
            CANONICAL_KEY: SURVIVOR_ID,
        },
        "candidates": {"candidate-elon-vance": {"candidate_id": "candidate-elon-vance"}},
    }
    approval = {
        "canonical_identity_key": CANONICAL_KEY,
        "survivor_candidate_id": SURVIVOR_ID,
        "retired_candidate_ids": ["candidate-elon-vance"],
        "expected_members": _expected_members(history),
    }
    state = tmp_path / "state"
    _write_state(state, history, aliases)
    history_before = state_snapshot(state)
    aliases_before = state_snapshot(state)

    plan = build_durable_id_merge_plan(history, aliases, approval)

    assert plan["status"] in {"invalid", "stale", "blocked"}
    report, changed = migrate_candidate_history(state, durable_merge_approvals=[approval])
    assert changed is False
    assert report["apply_blocked"] is True
    with pytest.raises(ValueError, match="blocked"):
        migrate_candidate_history(state, apply=True, durable_merge_approvals=[approval])
    current_aliases = read_state(state, "candidate_identity_aliases")
    assert stored_candidate_id_for_alias(current_aliases, "elon musk|ashlee vance") == "candidate-elon-vance"
    assert resolve_candidate_id({"identity_key": "elon musk|ashlee vance"}, current_aliases) == "candidate-elon-vance"
    assert state_snapshot(state) == history_before
    assert state_snapshot(state) == aliases_before


def test_handmade_redirect_without_audit_is_refused(tmp_path: Path) -> None:
    history = [record for record in woman_on_fire_history() if record.get("candidate_id") == SURVIVOR_ID]
    aliases = woman_on_fire_aliases()
    assert isinstance(aliases["aliases"], dict)
    assert isinstance(aliases["candidates"], dict)
    aliases["version"] = 2
    aliases["redirects"] = {RETIRED_ID: SURVIVOR_ID}
    aliases["durable_merges"] = []
    state = tmp_path / "state"
    _write_state(state, history, aliases)
    history_before = state_snapshot(state)
    aliases_before = state_snapshot(state)

    plan = build_durable_id_merge_plan(history, aliases, woman_on_fire_approval())

    assert plan["status"] == "stale"
    report, changed = migrate_candidate_history(state, durable_merge_approvals=[woman_on_fire_approval()])
    assert changed is False
    assert report["apply_blocked"] is True
    assert state_snapshot(state) == history_before
    assert state_snapshot(state) == aliases_before


def test_noncanonical_survivor_reapplies_and_collects_without_conflict(tmp_path: Path) -> None:
    history = [
        {
            "identity_key": "woman on fire|med sheila de liz",
            "title": "Woman on Fire",
            "primary_authors": ["Dr. med. Sheila de Liz"],
            "candidate_id": RETIRED_ID,
            "editor_status": "reject",
            "editor_note": REJECT_NOTE,
            "first_seen_date": "2026-06-10",
            "last_seen_date": "2026-06-10",
            "independent_signal_count": 1,
            "independent_signal_families": ["bestseller/ranking"],
            "source_evidence": [_evidence("Amazon.de Charts")],
        },
        {
            "identity_key": CANONICAL_KEY,
            "title": "Woman on Fire",
            "primary_authors": ["Sheila de Liz"],
            "candidate_id": SURVIVOR_ID,
            "first_seen_date": "2026-08-13",
            "last_seen_date": "2026-09-03",
            "independent_signal_count": 1,
            "independent_signal_families": ["bestseller/ranking"],
            "source_evidence": [_evidence("Amazon.de Charts")],
        },
    ]
    approval = {
        "canonical_identity_key": CANONICAL_KEY,
        "survivor_candidate_id": RETIRED_ID,
        "retired_candidate_ids": [SURVIVOR_ID],
        "expected_members": _expected_members(history),
    }
    state = tmp_path / "state"
    _write_state(state, history, woman_on_fire_aliases())

    plan = build_durable_id_merge_plan(history, woman_on_fire_aliases(), approval)

    assert plan["status"] == "ready"
    first_report, first_changed = migrate_candidate_history(state, apply=True, durable_merge_approvals=[approval])
    assert first_changed is True
    assert first_report["apply_blocked"] is False
    merged = read_state(state, "candidate_history")
    assert len(merged) == 1
    assert merged[0]["candidate_id"] == RETIRED_ID
    assert merged[0]["editor_status"] == "reject"
    assert merged[0]["editor_note"] == REJECT_NOTE

    second_report, second_changed = migrate_candidate_history(state, apply=True, durable_merge_approvals=[approval])
    assert second_changed is False
    assert second_report["apply_blocked"] is False
    assert second_report["durable_merges"][0]["status"] == "already_applied"

    table = validate_alias_table(read_state(state, "candidate_identity_aliases"))
    assert stored_candidate_id_for_alias(table, CANONICAL_KEY) == SURVIVOR_ID
    assert assign_alias(dict(table), CANONICAL_KEY, RETIRED_ID) is None
    assert resolve_candidate_id({"identity_key": CANONICAL_KEY}, table) == RETIRED_ID
    assert resolve_candidate_id({"identity_key": "woman on fire|med sheila de liz"}, table) == RETIRED_ID


def test_changed_editor_decision_after_preview_refuses_apply(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write_state(state, woman_on_fire_history(), woman_on_fire_aliases())
    approval = woman_on_fire_approval()

    preview, preview_changed = migrate_candidate_history(state, durable_merge_approvals=[approval])
    assert preview_changed is False
    assert preview["apply_blocked"] is False

    changed_history = woman_on_fire_history()
    for record in changed_history:
        if record.get("candidate_id") == SURVIVOR_ID:
            record["editor_status"] = "interested"
            record["editor_note"] = "A new decision after preview."
    seed_state(state, candidate_history=changed_history)
    history_before = state_snapshot(state)
    aliases_before = state_snapshot(state)

    stale_report, stale_changed = migrate_candidate_history(state, durable_merge_approvals=[approval])
    assert stale_changed is False
    assert stale_report["apply_blocked"] is True
    assert stale_report["durable_merges"][0]["status"] == "stale"
    with pytest.raises(ValueError, match="blocked"):
        migrate_candidate_history(state, apply=True, durable_merge_approvals=[approval])
    assert state_snapshot(state) == history_before
    assert state_snapshot(state) == aliases_before


def test_changed_evidence_shape_after_preview_refuses_apply(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write_state(state, woman_on_fire_history(), woman_on_fire_aliases())
    approval = woman_on_fire_approval()

    changed_history = woman_on_fire_history()
    for record in changed_history:
        if record.get("candidate_id") == RETIRED_ID:
            record["primary_authors"] = ["Dr. med. Sheila de Liz", "Co Author"]
    seed_state(state, candidate_history=changed_history)

    report, changed = migrate_candidate_history(state, durable_merge_approvals=[approval])
    assert changed is False
    assert report["apply_blocked"] is True
    assert report["durable_merges"][0]["status"] == "stale"


def test_emit_approval_round_trip(tmp_path: Path) -> None:
    approval = emit_durable_merge_approval(woman_on_fire_history(), woman_on_fire_aliases())
    assert approval["survivor_candidate_id"] == SURVIVOR_ID
    assert approval["retired_candidate_ids"] == [RETIRED_ID]
    assert len(approval["expected_members"]) == 2

    state = tmp_path / "state"
    _write_state(state, woman_on_fire_history(), woman_on_fire_aliases())
    report, changed = migrate_candidate_history(state, apply=True, durable_merge_approvals=[approval])
    assert changed is True
    assert report["apply_blocked"] is False


def test_emit_approval_refuses_without_pending_merge(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no pending durable-ID merge"):
        emit_durable_merge_approval(
            [
                {
                    "identity_key": "solo|author",
                    "title": "Solo",
                    "primary_authors": ["Author"],
                    "candidate_id": "candidate-solo",
                }
            ],
            None,
        )


def test_cli_emit_then_apply(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = tmp_path / "state"
    _write_state(state, woman_on_fire_history(), woman_on_fire_aliases())
    approval_path = tmp_path / "approval.json"

    emit_exit = main(
        ["migrate-candidate-history", "--state-dir", str(state), "--emit-merge-approval", str(approval_path)]
    )
    assert emit_exit == 0
    assert f"survivor={SURVIVOR_ID}" in capsys.readouterr().out
    emitted = json.loads(approval_path.read_text(encoding="utf-8"))
    assert emitted["survivor_candidate_id"] == SURVIVOR_ID
    assert len(emitted["expected_members"]) == 2

    apply_exit = main(
        [
            "migrate-candidate-history",
            "--state-dir",
            str(state),
            "--apply",
            "--durable-merge-approval",
            str(approval_path),
        ]
    )
    assert apply_exit == 0
    assert "candidate_history_changed=true" in capsys.readouterr().out


def _bound_approval(survivor: str, retired: list[str], history: list[dict[str, object]]) -> dict[str, object]:
    return {
        "canonical_identity_key": CANONICAL_KEY,
        "survivor_candidate_id": survivor,
        "retired_candidate_ids": list(retired),
        "expected_members": _expected_members(history),
    }


def _interrupted_after_alias_write(
    state: Path, history: list[dict[str, object]], aliases: dict[str, object], approval: dict[str, object]
) -> None:
    """Leave the state an apply interrupted after its alias write left before #1070.

    The redirect is recorded while history is still split. An apply is now one
    State Store transaction and cannot leave this state, but state written by
    an earlier version can still hold it, so it is built here from a completed
    apply's alias table.
    """
    applied = state.parent / f"{state.name}-applied"
    _write_state(applied, history, aliases)
    migrate_candidate_history(applied, apply=True, durable_merge_approvals=[approval])
    seed_state(state, candidate_identity_aliases=read_state(applied, "candidate_identity_aliases"))
    table = read_state(state, "candidate_identity_aliases")
    assert table["redirects"] == {RETIRED_ID: SURVIVOR_ID}


def test_retired_survivor_is_refused_without_mutation(tmp_path: Path) -> None:
    state = tmp_path / "state"
    history = woman_on_fire_history()
    _write_state(state, history, woman_on_fire_aliases())
    _interrupted_after_alias_write(state, history, woman_on_fire_aliases(), woman_on_fire_approval())

    changed_history = woman_on_fire_history()
    for record in changed_history:
        if record.get("candidate_id") == SURVIVOR_ID:
            record.pop("editor_status", None)
            record.pop("editor_note", None)
        if record.get("candidate_id") == RETIRED_ID:
            record["editor_status"] = "reject"
            record["editor_note"] = REJECT_NOTE
    seed_state(state, candidate_history=changed_history)
    approval = _bound_approval(RETIRED_ID, [SURVIVOR_ID], changed_history)
    table = read_state(state, "candidate_identity_aliases")
    history_before = state_snapshot(state)
    aliases_before = state_snapshot(state)

    with pytest.raises(ValueError, match="retired survivor"):
        emit_durable_merge_approval(changed_history, table)
    plan = build_durable_id_merge_plan(changed_history, table, approval)
    assert plan["status"] == "invalid"
    assert plan["reason"] == "approval names a retired survivor"
    report, changed = migrate_candidate_history(state, durable_merge_approvals=[approval])
    assert changed is False
    assert report["apply_blocked"] is True
    assert report["durable_merges"][0]["status"] == "invalid"
    with pytest.raises(ValueError, match="blocked"):
        migrate_candidate_history(state, apply=True, durable_merge_approvals=[approval])
    assert state_snapshot(state) == history_before
    assert state_snapshot(state) == aliases_before
    reloaded = validate_alias_table(read_state(state, "candidate_identity_aliases"))
    assert reloaded["redirects"] == {RETIRED_ID: SURVIVOR_ID}


def test_record_merge_refuses_cycles_and_retired_survivors() -> None:
    from autoscout.candidate_identity import record_durable_id_merge

    table = validate_alias_table(woman_on_fire_aliases())
    record_durable_id_merge(
        table,
        canonical_identity_key=CANONICAL_KEY,
        survivor_candidate_id=SURVIVOR_ID,
        retired_candidate_ids=[RETIRED_ID],
    )
    with pytest.raises(ValueError, match="cycle|retired"):
        record_durable_id_merge(
            table,
            canonical_identity_key=CANONICAL_KEY,
            survivor_candidate_id=RETIRED_ID,
            retired_candidate_ids=[SURVIVOR_ID],
        )
    reloaded = validate_alias_table(table)
    assert reloaded["redirects"] == {RETIRED_ID: SURVIVOR_ID}

    chained: dict[str, object] = {"version": 1, "aliases": {}, "candidates": {}}
    record_durable_id_merge(
        chained,
        canonical_identity_key="a|x",
        survivor_candidate_id="candidate-b",
        retired_candidate_ids=["candidate-a"],
    )
    record_durable_id_merge(
        chained,
        canonical_identity_key="a|x",
        survivor_candidate_id="candidate-c",
        retired_candidate_ids=["candidate-b"],
    )
    assert effective_candidate_id("candidate-a", chained) == "candidate-c"
    with pytest.raises(ValueError, match="cycle|retired"):
        record_durable_id_merge(
            chained,
            canonical_identity_key="a|x",
            survivor_candidate_id="candidate-a",
            retired_candidate_ids=["candidate-c"],
        )


def test_later_duplicate_merges_after_noncanonical_survivor(tmp_path: Path) -> None:
    later_id = "candidate-later000000000000000001"
    history = [
        {
            "identity_key": "woman on fire|med sheila de liz",
            "title": "Woman on Fire",
            "primary_authors": ["Dr. med. Sheila de Liz"],
            "candidate_id": RETIRED_ID,
            "editor_status": "reject",
            "editor_note": REJECT_NOTE,
            "first_seen_date": "2026-06-10",
            "last_seen_date": "2026-06-10",
            "independent_signal_count": 1,
            "independent_signal_families": ["bestseller/ranking"],
            "source_evidence": [_evidence("Amazon.de Charts")],
        },
        {
            "identity_key": CANONICAL_KEY,
            "title": "Woman on Fire",
            "primary_authors": ["Sheila de Liz"],
            "candidate_id": SURVIVOR_ID,
            "first_seen_date": "2026-08-13",
            "last_seen_date": "2026-09-03",
            "independent_signal_count": 1,
            "independent_signal_families": ["bestseller/ranking"],
            "source_evidence": [_evidence("Amazon.de Charts")],
        },
    ]
    first = _bound_approval(RETIRED_ID, [SURVIVOR_ID], history)
    state = tmp_path / "state"
    _write_state(state, history, woman_on_fire_aliases())
    first_report, first_changed = migrate_candidate_history(state, apply=True, durable_merge_approvals=[first])
    assert first_changed is True
    assert first_report["apply_blocked"] is False

    grown = read_state(state, "candidate_history")
    grown.append(
        {
            "identity_key": "woman on fire|sheila de liz paperback",
            "title": "Woman on Fire",
            "primary_authors": ["Sheila de Liz"],
            "candidate_id": later_id,
            "first_seen_date": "2026-09-04",
            "last_seen_date": "2026-09-04",
        }
    )
    seed_state(state, candidate_history=grown)
    table = read_state(state, "candidate_identity_aliases")

    second = emit_durable_merge_approval(grown, table)
    assert second["survivor_candidate_id"] == RETIRED_ID
    assert second["retired_candidate_ids"] == [later_id]
    preview, preview_changed = migrate_candidate_history(state, durable_merge_approvals=[second])
    assert preview_changed is False
    assert preview["apply_blocked"] is False
    assert preview["durable_merges"][0]["status"] == "ready"

    applied, applied_changed = migrate_candidate_history(state, apply=True, durable_merge_approvals=[second])
    assert applied_changed is True
    assert applied["apply_blocked"] is False
    merged = read_state(state, "candidate_history")
    assert len(merged) == 1
    assert merged[0]["candidate_id"] == RETIRED_ID
    assert merged[0]["editor_status"] == "reject"
    assert merged[0]["editor_note"] == REJECT_NOTE
    final_table = validate_alias_table(read_state(state, "candidate_identity_aliases"))
    assert stored_candidate_id_for_alias(final_table, CANONICAL_KEY) == SURVIVOR_ID
    assert final_table["redirects"] == {SURVIVOR_ID: RETIRED_ID, later_id: RETIRED_ID}
    assert resolve_candidate_id({"identity_key": "woman on fire|sheila de liz paperback"}, final_table) == RETIRED_ID
    assert assign_alias(dict(final_table), CANONICAL_KEY, RETIRED_ID) is None

    repeat, repeat_changed = migrate_candidate_history(state, apply=True, durable_merge_approvals=[second])
    assert repeat_changed is False
    assert repeat["durable_merges"][0]["status"] == "already_applied"
