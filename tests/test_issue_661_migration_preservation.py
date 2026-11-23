"""Unrelated-record and typed-evidence preservation (Issue #661).

The approved Woman on Fire durable-ID merge from #456 projected 829 history
records becoming 828 while silently rewriting 804 unrelated records: empty
lists became empty strings, null provider outcomes became empty strings,
repeated grounded-description attempts were removed, and some description
provenance changed.

This suite uses an isolated public CLI reproduction with the approved pair
plus an unrelated record carrying empty lists, nulls, nested evidence, and
repeated attempt entries. It asserts structural preservation of unrelated
records, typed preservation inside the merged group, zero-mutation preview,
and byte-identical repeated apply.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout.__main__ import main
from autoscout.candidate_history_migration import build_candidate_history_migration_plan
from autoscout.candidate_history_migration import migrate_candidate_history

RETIRED_ID = "candidate-ad10f4378f94afbe07d71f3a"
SURVIVOR_ID = "candidate-cad6745f4e85571d41274186"
UNRELATED_ID = "candidate-0e76d8cd44b850474171500c"
CANONICAL_KEY = "woman on fire|sheila de liz"
RETIRED_KEY = "woman on fire|med sheila de liz"
UNRELATED_KEY = "101 essays that will change the way you think|brianna wiest"
REJECT_NOTE = "Бестселлер про менопаузу. Книгу либо не продают в РФ."


def _unrelated_record() -> dict[str, object]:
    return {
        "identity_key": UNRELATED_KEY,
        "title": "101 Essays That Will Change The Way You Think",
        "primary_authors": ["Brianna Wiest"],
        "candidate_id": UNRELATED_ID,
        "first_seen_date": "2026-07-17",
        "last_seen_date": "2026-09-03",
        "independent_signal_count": 1,
        "independent_signal_families": ["audiobook ranking"],
        "source_evidence": [{"source_family": "audiobook ranking", "source_name": "Audiobooks"}],
        "russian_edition_evidence": [],
        "russian_edition_verification": {
            "check_coverage": "not_run",
            "checked_at": "2026-09-03",
            "checked_sources": [],
            "confidence": "low",
            "evidence_urls": [],
            "provider_outcome": None,
            "providers": [
                {
                    "checked_at": "2026-09-03",
                    "evidence": [],
                    "evidence_urls": [],
                    "name": "Ozon",
                    "outcome": None,
                    "reason": "Empty fixture is not proof of absence",
                    "result_count": 0,
                    "source_type": "russian-retailer",
                    "status": "not_run",
                }
            ],
            "query_timestamp": "2026-09-03",
            "reason": "no match",
        },
        "grounded_description_attempts": [
            {"date": "2026-08-15", "outcome": "attempted_no_description", "reason_code": "title_mismatch"},
            {
                "date": "2026-08-16",
                "outcome": "attempted_no_description",
                "reason_code": "insufficient_subject_text",
            },
            {
                "date": "2026-08-16",
                "outcome": "attempted_no_description",
                "reason_code": "insufficient_subject_text",
            },
            {
                "date": "2026-08-16",
                "outcome": "attempted_no_description",
                "reason_code": "source_identity_disagreement",
            },
        ],
        "description_provenance": {
            "applied_date": "2026-07-17",
            "content_description": "Сборник эссе.",
            "evidence_basis": [],
            "identity_key": UNRELATED_KEY,
            "provenance": "backfilled",
        },
    }


def _retired_record() -> dict[str, object]:
    return {
        "identity_key": RETIRED_KEY,
        "title": "Woman on Fire",
        "primary_authors": ["Dr. med. Sheila de Liz"],
        "candidate_id": RETIRED_ID,
        "first_seen_date": "2026-06-10",
        "last_seen_date": "2026-06-10",
        "independent_signal_count": 1,
        "independent_signal_families": ["bestseller/ranking"],
        "source_evidence": [{"source_family": "bestseller/ranking", "source_name": "Amazon"}],
        "russian_edition_verification": {
            "check_coverage": "not_run",
            "checked_at": "2026-08-30",
            "checked_sources": [],
            "confidence": "low",
            "evidence_urls": [],
            "provider_outcome": None,
            "providers": [],
            "query_timestamp": "2026-08-30",
            "reason": "No providers",
        },
        "grounded_description_attempts": [
            {"date": "2026-08-16", "outcome": "attempted_no_description", "reason_code": "ambiguity"}
        ],
        "description_provenance": {
            "applied_date": "2026-08-26",
            "content_description": "Медицинский путеводитель.",
            "evidence_basis": ["https://www.rowohlt.de/buch/woman-on-fire"],
            "identity_key": RETIRED_KEY,
            "provenance": "backfilled",
        },
    }


def _survivor_record() -> dict[str, object]:
    return {
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
        "source_evidence": [{"source_family": "bestseller/ranking", "source_name": "Amazon"}],
        "russian_edition_evidence": [],
        "russian_edition_verification": {
            "check_coverage": "not_run",
            "checked_at": "2026-09-03",
            "checked_sources": [],
            "confidence": "low",
            "evidence_urls": [],
            "provider_outcome": None,
            "providers": [
                {
                    "checked_at": "2026-09-03",
                    "evidence": [],
                    "evidence_urls": [],
                    "name": "Ozon",
                    "outcome": None,
                    "reason": "Empty fixture",
                    "result_count": 0,
                    "source_type": "russian-retailer",
                    "status": "not_run",
                }
            ],
            "query_timestamp": "2026-09-03",
            "reason": "no match",
        },
        "grounded_description_attempts": [
            {
                "date": "2026-08-16",
                "outcome": "attempted_no_description",
                "reason_code": "source_identity_disagreement",
            },
            {
                "date": "2026-09-03",
                "outcome": "not_attempted",
                "reason_code": "not_attempted_stage_budget",
            },
        ],
        "description_provenance": {
            "applied_date": "2026-08-13",
            "content_description": "Популярное руководство.",
            "evidence_basis": ["https://www.amazon.de/dp/B081SJK2C9"],
            "identity_key": CANONICAL_KEY,
            "provenance": "codex-generated",
        },
    }


def _history() -> list[dict[str, object]]:
    return [_retired_record(), _survivor_record(), _unrelated_record()]


def _aliases() -> dict[str, object]:
    return {
        "version": 1,
        "aliases": {
            RETIRED_KEY: RETIRED_ID,
            CANONICAL_KEY: SURVIVOR_ID,
            UNRELATED_KEY: UNRELATED_ID,
        },
        "candidates": {
            RETIRED_ID: {"candidate_id": RETIRED_ID},
            SURVIVOR_ID: {"candidate_id": SURVIVOR_ID},
            UNRELATED_ID: {"candidate_id": UNRELATED_ID},
        },
    }


def _expected_members(history: list[dict[str, object]]) -> list[dict[str, object]]:
    members: list[dict[str, object]] = []
    for record in history[:2]:
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


def _approval(history: list[dict[str, object]] | None = None) -> dict[str, object]:
    live = history or _history()
    return {
        "canonical_identity_key": CANONICAL_KEY,
        "survivor_candidate_id": SURVIVOR_ID,
        "retired_candidate_ids": [RETIRED_ID],
        "expected_members": _expected_members(live),
    }


def _write_state(state: Path, history: list[dict[str, object]], aliases: dict[str, object]) -> None:
    state.mkdir(parents=True, exist_ok=True)
    seed_state(state, candidate_history=history)
    seed_state(state, candidate_identity_aliases=aliases)


def _stored_index(state: Path, identity_key: str) -> int:
    """Position of a record in Candidate History as the State Store stores it (the store sorts records)."""
    return [record["identity_key"] for record in read_state(state, "candidate_history")].index(identity_key)


def test_unrelated_singleton_preserves_types_and_repeated_attempts() -> None:
    plan = build_candidate_history_migration_plan([_unrelated_record()])

    assert plan["singleton_changed_count"] == 0
    assert plan["singleton_changed_details"] == []
    projected = plan["projected_history"][0]
    original = _unrelated_record()

    assert projected == original
    assert projected["russian_edition_evidence"] == []
    assert isinstance(projected["russian_edition_evidence"], list)
    verification = projected["russian_edition_verification"]
    assert isinstance(verification, dict)
    assert verification["checked_sources"] == []
    assert verification["evidence_urls"] == []
    assert verification["provider_outcome"] is None
    assert len(projected["grounded_description_attempts"]) == 4
    assert projected["grounded_description_attempts"] == original["grounded_description_attempts"]
    provenance = projected["description_provenance"]
    assert isinstance(provenance, dict)
    assert provenance["evidence_basis"] == []


def test_approved_merge_preserves_typed_evidence_with_zero_singleton_changes(tmp_path: Path) -> None:
    state = tmp_path / "state"
    history = _history()
    _write_state(state, history, _aliases())
    history_before = state_snapshot(state)
    aliases_before = state_snapshot(state)

    report, changed = migrate_candidate_history(state, durable_merge_approvals=[_approval(history)])

    assert changed is False
    assert report["apply_blocked"] is False
    assert report["history_entries_before"] == 3
    assert report["projected_history_count"] == 2
    assert report["singleton_changed_count"] == 0
    assert report["singleton_changed_details"] == []
    assert report["durable_merges"][0]["status"] == "ready"
    assert state_snapshot(state) == history_before
    assert state_snapshot(state) == aliases_before

    unrelated_projected = next(
        item for item in report["projected_history"] if item.get("identity_key") == UNRELATED_KEY
    )
    assert unrelated_projected == _unrelated_record()

    merged = next(item for item in report["projected_history"] if item.get("identity_key") == CANONICAL_KEY)
    assert merged["candidate_id"] == SURVIVOR_ID
    assert merged["editor_status"] == "reject"
    assert merged["editor_note"] == REJECT_NOTE
    assert merged["russian_edition_evidence"] == []
    assert isinstance(merged["russian_edition_evidence"], list)
    merged_verification = merged["russian_edition_verification"]
    assert isinstance(merged_verification, dict)
    assert merged_verification["checked_sources"] == []
    assert merged_verification["evidence_urls"] == []
    assert merged_verification["provider_outcome"] is None
    assert len(merged["grounded_description_attempts"]) == 3
    assert merged["first_seen_date"] == "2026-06-10"
    assert merged["last_seen_date"] == "2026-09-03"


def test_cli_dry_run_then_apply_is_byte_idempotent(tmp_path: Path, capsys) -> None:
    state = tmp_path / "state"
    history = _history()
    _write_state(state, history, _aliases())
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(json.dumps(_approval(history), ensure_ascii=False), encoding="utf-8")
    history_before = state_snapshot(state)
    aliases_before = state_snapshot(state)

    dry_exit = main(
        ["migrate-candidate-history", "--state-dir", str(state), "--durable-merge-approval", str(approval_path)]
    )
    dry_out = capsys.readouterr().out

    assert dry_exit == 0
    assert "candidate_history_migration=dry-run" in dry_out
    assert "singleton_changed_count=0" in dry_out
    assert "apply_blocked=false" in dry_out
    assert state_snapshot(state) == history_before
    assert state_snapshot(state) == aliases_before

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
    stored = read_state(state, "candidate_history")
    assert len(stored) == 2
    unrelated_stored = next(item for item in stored if item.get("identity_key") == UNRELATED_KEY)
    assert unrelated_stored == _unrelated_record()
    merged_stored = next(item for item in stored if item.get("identity_key") == CANONICAL_KEY)
    assert merged_stored["russian_edition_evidence"] == []
    assert merged_stored["russian_edition_verification"]["provider_outcome"] is None
    assert len(merged_stored["grounded_description_attempts"]) == 3

    history_bytes = state_snapshot(state)
    aliases_bytes = state_snapshot(state)

    second_report, second_changed = migrate_candidate_history(
        state, apply=True, durable_merge_approvals=[_approval(_history())]
    )

    # The stored unrelated record is structurally preserved, so only the
    # already-recorded merge remains. Re-applying the same approval must not
    # rewrite bytes.
    assert second_changed is False
    assert second_report["durable_merges"][0]["status"] == "already_applied"
    assert state_snapshot(state) == history_bytes
    assert state_snapshot(state) == aliases_bytes


def test_merge_nonempty_values_preserves_blank_types_and_audit_order() -> None:
    from autoscout.candidate_history_migration import _merge_nonempty_values

    assert _merge_nonempty_values([[]]) == []
    assert _merge_nonempty_values([None]) is None
    assert _merge_nonempty_values([{}]) == {}
    assert _merge_nonempty_values([""]) == ""
    repeated = [{"date": "2026-08-16", "reason_code": "x"}, {"date": "2026-08-16", "reason_code": "x"}]
    merged = _merge_nonempty_values([copy.deepcopy(repeated)])
    assert merged == repeated
    assert len(merged) == 2
    nested = _merge_nonempty_values([{"checked_sources": [], "provider_outcome": None}])
    assert nested == {"checked_sources": [], "provider_outcome": None}


def test_singleton_preserves_empty_editor_fields_without_rekey(tmp_path: Path) -> None:
    """P2-1: empty editor_status/note on an unrelated singleton survive the approved merge."""
    unrelated = _unrelated_record()
    unrelated["editor_status"] = ""
    unrelated["editor_note"] = ""
    history = [_retired_record(), _survivor_record(), unrelated]
    state = tmp_path / "state"
    _write_state(state, history, _aliases())

    report, changed = migrate_candidate_history(state, durable_merge_approvals=[_approval(history)])

    assert changed is False
    assert report["apply_blocked"] is False
    assert report["singleton_changed_count"] == 0
    unrelated_projected = next(
        item for item in report["projected_history"] if item.get("identity_key") == UNRELATED_KEY
    )
    assert "editor_status" in unrelated_projected
    assert "editor_note" in unrelated_projected
    assert unrelated_projected["editor_status"] == ""
    assert unrelated_projected["editor_note"] == ""
    assert unrelated_projected == unrelated


def test_missing_candidate_id_is_reported_before_apply(tmp_path: Path) -> None:
    """P2-2: scope is calculated after seeding, and missing differs from null."""
    from autoscout.candidate_history_migration import _singleton_changed_details

    unrelated = _unrelated_record()
    del unrelated["candidate_id"]
    history = [_retired_record(), _survivor_record(), unrelated]
    state = tmp_path / "state"
    aliases = _aliases()
    assert isinstance(aliases["aliases"], dict)
    aliases["aliases"].pop(UNRELATED_KEY, None)
    _write_state(state, history, aliases)

    report, changed = migrate_candidate_history(state, durable_merge_approvals=[_approval(history)])

    assert changed is False
    assert report["singleton_changed_count"] == 1
    detail = report["singleton_changed_details"][0]
    assert detail["history_index"] == _stored_index(state, UNRELATED_KEY)
    assert "candidate_id" in detail["changed_fields"]
    unrelated_projected = next(
        item for item in report["projected_history"] if item.get("identity_key") == UNRELATED_KEY
    )
    assert "candidate_id" in unrelated_projected

    # Missing key must not compare equal to an explicit null.
    records = [{"identity_key": "a|x", "title": "A", "primary_authors": ["X"]}]
    groups = [[0]]
    details = [{"member_count": 1, "canonical_history_index": 0}]
    with_null = [{"identity_key": "a|x", "title": "A", "primary_authors": ["X"], "editor_note": None}]
    assert _singleton_changed_details(records, groups, details, with_null)[0]["changed_fields"] == ["editor_note"]
    identical = [{"identity_key": "a|x", "title": "A", "primary_authors": ["X"]}]
    assert _singleton_changed_details(records, groups, details, identical) == []


def test_unrekeyable_legacy_record_scope_includes_seeded_id(tmp_path: Path, capsys) -> None:
    """P2 legacy scope: unrekeyable singletons appear in preview before apply adds the ID."""
    orphan: dict[str, object] = {"identity_key": "orphan legacy record"}
    history = [_retired_record(), _survivor_record(), orphan]
    state = tmp_path / "state"
    aliases: dict[str, object] = {
        "version": 1,
        "aliases": {RETIRED_KEY: RETIRED_ID, CANONICAL_KEY: SURVIVOR_ID},
        "candidates": {},
    }
    _write_state(state, history, aliases)
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(json.dumps(_approval(history), ensure_ascii=False), encoding="utf-8")
    history_before = state_snapshot(state)

    dry_exit = main(
        ["migrate-candidate-history", "--state-dir", str(state), "--durable-merge-approval", str(approval_path)]
    )
    dry_out = capsys.readouterr().out

    assert dry_exit == 0
    assert "singleton_changed_count=1" in dry_out
    assert "apply_blocked=false" in dry_out
    assert state_snapshot(state) == history_before

    report, _ = migrate_candidate_history(state, durable_merge_approvals=[_approval(history)])
    assert report["singleton_changed_count"] == 1
    detail = report["singleton_changed_details"][0]
    assert detail["history_index"] == _stored_index(state, "orphan legacy record")
    assert detail["changed_fields"] == ["candidate_id"]

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
    stored = read_state(state, "candidate_history")
    orphan_stored = next(item for item in stored if item.get("identity_key") == "orphan legacy record")
    assert "candidate_id" in orphan_stored
    assert isinstance(orphan_stored["candidate_id"], str)
    assert orphan_stored["candidate_id"] != ""
