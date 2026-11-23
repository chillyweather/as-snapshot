"""Deterministic Candidate History identity migration (Issue #439).

The migration is deliberately limited to Candidate History.
It uses the same raw-field identity seams as the weekly pipeline and never
consults the network, a model, or other state files.
"""

from __future__ import annotations

import copy
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from autoscout.candidate_identity import candidate_id_for
from autoscout.candidate_identity import effective_candidate_id
from autoscout.candidate_identity import record_durable_id_merge
from autoscout.candidate_identity import seed_alias_table_from_migration
from autoscout.candidate_identity import stored_candidate_id_for_alias
from autoscout.candidate_identity import validate_alias_table
from autoscout.editor_decisions import DECISION_LOG_FIELD
from autoscout.editor_decisions import merge_decision_logs
from autoscout.identity import AUTHOR_LIST_SEPARATOR
from autoscout.identity import _match_shared_author_pair
from autoscout.identity import normalize_author_display
from autoscout.identity import normalized_contributor_set
from autoscout.identity import normalized_identity_key
from autoscout.identity import pick_canonical_mention
from autoscout.pipeline import _explicit_coauthor_bridge_mention
from autoscout.state_store import Collection
from autoscout.state_store import StateStore
from autoscout.state_store import open_state_store
from autoscout.title_evidence import merge_title_metadata


def _text(value: Any) -> str:
    return str(value or "").strip()


def _nonblank(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str | list | tuple | dict | set):
        return bool(value)
    return True


def _raw_mention(record: dict[str, Any]) -> dict[str, Any]:
    authors = record.get("primary_authors")
    if not isinstance(authors, list):
        authors = []
    return {"title": record.get("title", ""), "primary_authors": authors}


def _is_rekeyable(record: dict[str, Any]) -> bool:
    mention = _raw_mention(record)
    return bool(_text(mention["title"]) and normalized_contributor_set(mention))


def _stable_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _merge_nonempty_values(values: list[Any]) -> Any:
    """Merge values preserving types, empty values, and ordered audit history.

    Issue #661: empty lists stay empty lists, null stays null, empty mappings
    stay mappings, and list members are concatenated in stable input order
    with duplicates preserved, so repeated grounded-description attempts and
    other ordered audit history are not lost. When no value is nonblank, the
    first value is returned unchanged to preserve its type instead of
    collapsing to an empty string.
    """
    if not values:
        return ""
    first = next((value for value in values if _nonblank(value)), None)
    if first is None:
        # Every value is blank (``[]``, ``{}``, ``None``, ``""``, ``False``).
        # ``None`` can never be the ``first`` nonblank above, so reaching here
        # proves there is no nonblank value. Preserve the first input's type.
        return copy.deepcopy(values[0])
    if isinstance(first, list):
        return [copy.deepcopy(item) for value in values if isinstance(value, list) for item in value]
    if isinstance(first, dict):
        result: dict[str, Any] = {}
        keys = sorted({key for value in values if isinstance(value, dict) for key in value}, key=str)
        for key in keys:
            result[key] = _merge_nonempty_values(
                [value[key] for value in values if isinstance(value, dict) and key in value]
            )
        return result
    return copy.deepcopy(first)


def _date_extreme(records: list[dict[str, Any]], field: str, *, latest: bool) -> Any:
    values = [_text(record.get(field)) for record in records if _text(record.get(field))]
    if not values:
        return None
    return (max if latest else min)(values)


def _editor_note_value(value: Any) -> str:
    """Return a nonblank editor note without changing its text."""
    if value is None:
        return ""
    note = str(value)
    return note if note.strip() else ""


def _editor_metadata(records: list[dict[str, Any]]) -> tuple[list[str], list[str], list[dict[str, str]]]:
    statuses = [_text(record.get("editor_status")) for record in records if _text(record.get("editor_status"))]
    notes = [_editor_note_value(record.get("editor_note")) for record in records]
    notes = [note for note in notes if note]
    decisions = [
        {"status": _text(record.get("editor_status")), "note": _editor_note_value(record.get("editor_note"))}
        for record in records
        if _text(record.get("editor_status")) or _editor_note_value(record.get("editor_note"))
    ]
    return list(dict.fromkeys(statuses)), list(dict.fromkeys(notes)), decisions


def _editor_conflicts(records: list[dict[str, Any]]) -> list[str]:
    """Return hard conflicts that make applying a merged editor decision unsafe."""
    statuses, _notes, _decisions = _editor_metadata(records)
    conflicts: list[str] = []
    if len(statuses) > 1:
        conflicts.append("multiple_editor_statuses")
    # A note without a status cannot be safely associated with an editor decision
    # after records merge, so it remains a hard apply conflict.
    if any(
        _editor_note_value(record.get("editor_note")) and not _text(record.get("editor_status")) for record in records
    ):
        conflicts.append("editor_note_without_status")
    return conflicts


def _expected_member_binding(value: Any) -> dict[str, Any]:
    """Clean one approval member binding without touching state."""
    if not isinstance(value, dict):
        raise ValueError("durable merge approval members must be objects")
    candidate_id = value.get("candidate_id", "")
    identity_key = value.get("identity_key", "")
    title = value.get("title", "")
    authors = value.get("primary_authors", [])
    status = value.get("editor_status", "")
    note = value.get("editor_note", "")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise ValueError("durable merge approval members need candidate IDs")
    if not isinstance(identity_key, str) or not isinstance(title, str):
        raise ValueError("durable merge approval members need identity keys and titles")
    if not isinstance(authors, list) or not all(isinstance(item, str) for item in authors):
        raise ValueError("durable merge approval members need primary author lists")
    if not isinstance(status, str) or not isinstance(note, str):
        raise ValueError("durable merge approval members need editor status and note strings")
    return {
        "candidate_id": candidate_id.strip(),
        "identity_key": identity_key,
        "title": title,
        "primary_authors": list(authors),
        "editor_status": _text(status),
        "editor_note": _editor_note_value(note),
    }


def _live_member_binding(record: dict[str, Any]) -> dict[str, Any] | None:
    """Return the binding shape for a live history record, or None when it cannot match."""
    candidate_id = _text(record.get("candidate_id"))
    identity_key = record.get("identity_key", "")
    title = record.get("title", "")
    authors = record.get("primary_authors", [])
    status = record.get("editor_status", "")
    note = record.get("editor_note", "")
    if not candidate_id or not isinstance(identity_key, str) or not isinstance(title, str):
        return None
    if not isinstance(authors, list) or not all(isinstance(item, str) for item in authors):
        return None
    if not isinstance(status, str) and status is not None:
        return None
    if not isinstance(note, str) and note is not None:
        return None
    return {
        "candidate_id": candidate_id,
        "identity_key": identity_key,
        "title": title,
        "primary_authors": list(authors),
        "editor_status": _text(status),
        "editor_note": _editor_note_value(note if isinstance(note, str) else ""),
    }


def validate_durable_merge_approval(value: Any) -> dict[str, Any]:
    """Validate an explicit durable-ID merge approval without touching state.

    The approval binds the exact reviewed records: every live member must
    match ``expected_members`` verbatim before anything is written, so a
    changed editor decision or reshaped record refuses as stale.
    """
    if not isinstance(value, dict):
        raise ValueError("durable merge approval must be an object")
    canonical = value.get("canonical_identity_key", "")
    survivor = value.get("survivor_candidate_id", "")
    retired = value.get("retired_candidate_ids", [])
    expected = value.get("expected_members", [])
    if not isinstance(canonical, str) or not canonical.strip():
        raise ValueError("durable merge approval needs a canonical identity key")
    if not isinstance(survivor, str) or not survivor.strip():
        raise ValueError("durable merge approval needs a survivor candidate ID")
    if not isinstance(retired, list) or not retired:
        raise ValueError("durable merge approval needs retired candidate IDs")
    clean_retired = sorted({str(item).strip() for item in retired if str(item).strip()})
    if len(clean_retired) != len(retired) or any(not str(item).strip() for item in retired):
        raise ValueError("durable merge approval retired IDs must be distinct nonblank strings")
    if survivor.strip() in clean_retired:
        raise ValueError("durable merge approval survivor must not be retired")
    if len(clean_retired) != 1:
        raise ValueError("only pairwise durable merges are supported")
    if not isinstance(expected, list) or not expected:
        raise ValueError("durable merge approval needs expected members")
    clean_expected = [_expected_member_binding(item) for item in expected]
    member_ids = sorted({item["candidate_id"] for item in clean_expected})
    if member_ids != sorted({survivor.strip(), *clean_retired}):
        raise ValueError("durable merge approval members must name exactly the approved IDs")
    return {
        "canonical_identity_key": canonical.strip(),
        "survivor_candidate_id": survivor.strip(),
        "retired_candidate_ids": clean_retired,
        "expected_members": clean_expected,
    }


def _merge_audit_recorded(table: dict[str, Any], canonical: str, survivor: str, retired: list[str]) -> bool:
    """Return whether the alias table already records this exact merge."""
    merges = table.get("durable_merges", [])
    if not isinstance(merges, list):
        return False
    for entry in merges:
        if not isinstance(entry, dict):
            continue
        if (
            str(entry.get("canonical_identity_key", "")) == canonical
            and str(entry.get("survivor_candidate_id", "")) == survivor
            and sorted(entry.get("retired_candidate_ids", [])) == sorted(retired)
        ):
            return True
    return False


def _durable_merge_guard(members: list[dict[str, Any]]) -> dict[str, Any]:
    """Check the #434 same-title plus shared-author rule for a durable merge.

    Every cross-ID pair must share the normalized work title and at least one
    normalized author. ISBN equality never approves a durable merge on its own,
    so the #559 different-title case cannot pass this guard.
    """
    by_id: dict[str, list[dict[str, Any]]] = {}
    for record in members:
        by_id.setdefault(_text(record.get("candidate_id")), []).append(record)
    ids = sorted(by_id)
    shared: list[str] = []
    passed = True
    title = ""
    if len(ids) == 2:
        mentions_a = [_raw_mention(record) for record in by_id[ids[0]]]
        mentions_b = [_raw_mention(record) for record in by_id[ids[1]]]
        from autoscout.identity import normalized_title as _normalized_title

        titles = {_normalized_title(mention) for mention in mentions_a + mentions_b}
        titles.discard("")
        if len(titles) != 1:
            passed = False
        else:
            title = next(iter(titles))
            for mention_a in mentions_a:
                for mention_b in mentions_b:
                    if not _match_shared_author_pair(mention_a, mention_b):
                        passed = False
            if passed:
                contributors_a: set[str] = set()
                for mention in mentions_a:
                    contributors_a.update(normalized_contributor_set(mention))
                contributors_b: set[str] = set()
                for mention in mentions_b:
                    contributors_b.update(normalized_contributor_set(mention))
                shared = sorted(contributors_a & contributors_b)
                if not shared:
                    passed = False
    else:
        passed = False
    return {"passed": passed, "normalized_title": title, "shared_authors": shared}


def _durable_merge_survivor(members: list[dict[str, Any]], canonical_key: str) -> tuple[str, list[str], str]:
    """Select the surviving ID without moving an editor decision.

    When exactly one side carries editor status or a note, that side survives
    so a decision is never reattached to a different durable ID. Otherwise the
    choice is deterministic: the deterministic ID for the canonical key when it
    matches one side, else the smallest ID. Conflicting editor decisions have
    no safe survivor and raise.
    """
    by_id: dict[str, list[dict[str, Any]]] = {}
    for record in members:
        record_id = _text(record.get("candidate_id"))
        if not record_id:
            raise ValueError("durable merge members must carry durable candidate IDs")
        by_id.setdefault(record_id, []).append(record)
    if len(by_id) != 2:
        raise ValueError("only pairwise durable merges are supported")
    ordered_ids = sorted(by_id)
    carriers = sorted(
        record_id
        for record_id, side in by_id.items()
        if any(_text(item.get("editor_status")) or _editor_note_value(item.get("editor_note")) for item in side)
    )
    if len(carriers) == 1:
        survivor = carriers[0]
        rationale = "survivor carries the only editor decision"
    else:
        deterministic = candidate_id_for(canonical_key)
        if deterministic in by_id:
            survivor = deterministic
            rationale = "survivor is the deterministic ID for the canonical key"
        else:
            survivor = ordered_ids[0]
            rationale = "survivor is the smallest candidate ID"
    retired = [item for item in ordered_ids if item != survivor]
    conflicts = _editor_conflicts(members)
    if conflicts:
        raise ValueError(f"durable merge has unsafe editor metadata: {', '.join(conflicts)}")
    return survivor, retired, rationale


def build_durable_id_merge_plan(
    candidate_history: list[dict[str, Any]],
    existing_alias_table: dict[str, Any] | None,
    approval: dict[str, Any],
) -> dict[str, Any]:
    """Build a pure preview for one approved durable-ID merge.

    The inputs are copied, never mutated. The plan names every affected durable
    ID and identity key, the survivor and why it was chosen, the preserved
    editor state verbatim, any conflicts, and the stored versus effective alias
    effects. A plan with status other than ``ready`` or ``already_applied``
    must not be applied.
    """
    cleaned = validate_durable_merge_approval(approval)
    canonical = cleaned["canonical_identity_key"]
    approved_survivor = cleaned["survivor_candidate_id"]
    approved_retired = list(cleaned["retired_candidate_ids"])
    records = [dict(record) for record in candidate_history] if isinstance(candidate_history, list) else []
    table = validate_alias_table(copy.deepcopy(existing_alias_table))
    members = [
        record for record in records if _text(record.get("candidate_id")) in {approved_survivor, *approved_retired}
    ]
    member_keys = sorted({_text(record.get("identity_key")) for record in members if _text(record.get("identity_key"))})
    aliases_of_interest = sorted(
        {
            alias
            for alias, stored in table.get("aliases", {}).items()
            if str(stored) in {approved_survivor, *approved_retired}
        }
        | {key for key in member_keys if key}
    )
    stored_effects = [
        {
            "alias": alias,
            "stored_candidate_id": stored_candidate_id_for_alias(table, alias),
            "effective_candidate_id": (
                effective_candidate_id(str(stored_candidate_id_for_alias(table, alias) or ""), table)
                if stored_candidate_id_for_alias(table, alias)
                else None
            ),
            "effective_after_merge": approved_survivor
            if stored_candidate_id_for_alias(table, alias) in {approved_survivor, *approved_retired}
            else None,
        }
        for alias in aliases_of_interest
    ]
    base: dict[str, Any] = {
        "canonical_identity_key": canonical,
        "survivor_candidate_id": approved_survivor,
        "retired_candidate_ids": list(approved_retired),
        "affected_candidate_ids": sorted({approved_survivor, *approved_retired}),
        "affected_identity_keys": member_keys,
        "member_count": len(members),
        "aliases_of_interest": aliases_of_interest,
        "alias_effects": stored_effects,
    }
    redirects = table.get("redirects", {}) if isinstance(table.get("redirects", {}), dict) else {}
    already_redirected = all(redirects.get(item) == approved_survivor for item in approved_retired)
    audited = _merge_audit_recorded(table, canonical, approved_survivor, approved_retired)
    present_ids = sorted({_text(record.get("candidate_id")) for record in members if _text(record.get("candidate_id"))})
    if not members:
        base["status"] = "stale"
        base["reason"] = "approved IDs are absent from current history"
        base["editor_decisions"] = []
        base["editor_status_conflicts"] = []
        base["unsafe_editor_metadata"] = ["stale_approval"]
        base["guard"] = {"passed": False, "normalized_title": "", "shared_authors": []}
        return base
    if present_ids == [approved_survivor]:
        # The retired records are gone. Without the recorded merge audit this
        # proves nothing about the missing side, so it is refused rather than
        # assumed to be a partially completed merge.
        survivor_records = [record for record in members if _text(record.get("candidate_id")) == approved_survivor]
        _s_statuses, s_notes, s_decisions = _editor_metadata(survivor_records)
        base["editor_decisions"] = s_decisions
        base["preserved_editor_notes"] = list(s_notes)
        base["editor_status_conflicts"] = []
        base["unsafe_editor_metadata"] = []
        base["guard"] = {"passed": False, "normalized_title": "", "shared_authors": []}
        if (
            audited
            and already_redirected
            and any(_text(record.get("identity_key")) == canonical for record in survivor_records)
        ):
            base["status"] = "already_applied"
            base["reason"] = "merge already recorded"
            base["guard"] = {"passed": True, "normalized_title": "", "shared_authors": []}
        else:
            base["status"] = "stale"
            base["reason"] = "retired history records are absent without a recorded merge"
            base["unsafe_editor_metadata"] = ["stale_approval"]
        return base
    guard = _durable_merge_guard(members)
    base["guard"] = guard
    _statuses, _notes, decisions = _editor_metadata(members)
    base["editor_decisions"] = decisions
    base["preserved_editor_notes"] = list(_notes)
    conflicts = _editor_conflicts(members)
    base["editor_status_conflicts"] = [status for status in _statuses if len(_statuses) > 1]
    base["unsafe_editor_metadata"] = list(conflicts)
    if not guard["passed"]:
        base["status"] = "invalid"
        base["reason"] = "identity guard failed"
        return base
    if conflicts:
        base["status"] = "blocked"
        base["reason"] = f"unsafe editor metadata: {', '.join(conflicts)}"
        return base
    try:
        expected_survivor, expected_retired, rationale = _durable_merge_survivor(members, canonical)
    except ValueError as exc:
        base["status"] = "blocked"
        base["reason"] = str(exc)
        return base
    base["expected_survivor_candidate_id"] = expected_survivor
    base["expected_retired_candidate_ids"] = list(expected_retired)
    base["survivor_rationale"] = rationale
    if expected_survivor != approved_survivor or sorted(expected_retired) != sorted(approved_retired):
        base["status"] = "invalid"
        base["reason"] = "approval names the wrong survivor"
        return base
    current_redirects = table.get("redirects", {})
    if not isinstance(current_redirects, dict):
        current_redirects = {}
    if approved_survivor in current_redirects:
        base["status"] = "invalid"
        base["reason"] = "approval names a retired survivor"
        return base
    projected_redirects = dict(current_redirects)
    for retired_id in approved_retired:
        projected_redirects[retired_id] = approved_survivor
    for start, target in projected_redirects.items():
        chain = {start}
        cursor: str | None = target
        while cursor is not None and cursor in projected_redirects:
            if cursor in chain:
                base["status"] = "invalid"
                base["reason"] = "approval would create a redirect cycle"
                return base
            chain.add(cursor)
            cursor = projected_redirects.get(cursor)
    live_bindings = [_live_member_binding(record) for record in members]
    expected_bindings = sorted(
        (_stable_value(item) for item in cleaned["expected_members"]),
    )
    live_markers = sorted(_stable_value(item) for item in live_bindings if item is not None)
    if len(live_markers) != len(members) or live_markers != expected_bindings:
        base["status"] = "stale"
        base["reason"] = "approval does not match current records"
        base["unsafe_editor_metadata"] = [*conflicts, "stale_approval"]
        return base
    if _text(members[0].get("identity_key")) and canonical not in member_keys:
        rekeyed = {normalized_identity_key(_raw_mention(record)) for record in members}
        if canonical not in rekeyed:
            base["status"] = "stale"
            base["reason"] = "canonical key does not match current members"
            return base
    base["status"] = "ready"
    base["reason"] = rationale
    return base


def _union_primary_authors(members: list[dict[str, Any]], canonical_position: int) -> list[str]:
    """Return canonical-first displays for all normalized contributors.

    Issue #444: merged records (more than one member) deliberately store
    ``normalize_author_display`` output instead of raw displays, so wrapper
    forms such as ``Mme ...`` and ``... MD`` collapse onto the wrapper-free
    natural shape. Records that are not actually merged keep their raw
    ordinary candidate displays unchanged. The union order stays
    canonical-member-first, then stable input order, and every normalized
    contributor is kept exactly once.
    """
    merged = len(members) > 1
    traversal = [canonical_position, *[index for index in range(len(members)) if index != canonical_position]]
    authors: list[str] = []
    seen: set[str] = set()
    for member_position in traversal:
        raw_authors = members[member_position].get("primary_authors", [])
        if not isinstance(raw_authors, list):
            continue
        for raw_author in raw_authors:
            # Split exactly as the contributor set does, so each display is
            # one person and the set below holds at most one (issue #756).
            for component in AUTHOR_LIST_SEPARATOR.split(str(raw_author)):
                display = component.strip()
                if not display:
                    continue
                normalized = next(iter(normalized_contributor_set({"primary_authors": [display]})), "")
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    authors.append(normalize_author_display(display) if merged else display)
    return authors


def _canonical_index(records: list[dict[str, Any]]) -> int:
    mentions = [_raw_mention(record) for record in records]
    bridge = _explicit_coauthor_bridge_mention(mentions)
    if bridge is not None:
        return next(index for index, mention in enumerate(mentions) if mention is bridge)
    canonical = pick_canonical_mention(mentions)
    return next(index for index, mention in enumerate(mentions) if mention is canonical)


def _union_groups(records: list[dict[str, Any]], rekeyable_indices: list[int]) -> list[list[int]]:
    parent = {index: index for index in rekeyable_indices}

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for position, left in enumerate(rekeyable_indices):
        for right in rekeyable_indices[position + 1 :]:
            if _match_shared_author_pair(_raw_mention(records[left]), _raw_mention(records[right])):
                union(left, right)

    groups: dict[int, list[int]] = {}
    for index in rekeyable_indices:
        groups.setdefault(find(index), []).append(index)
    return sorted((sorted(group) for group in groups.values()), key=lambda group: group[0])


def _merge_group(records: list[dict[str, Any]], indices: list[int]) -> tuple[dict[str, Any], dict[str, Any]]:
    members = [records[index] for index in indices]
    canonical_position = _canonical_index(members)
    canonical = members[canonical_position]
    projected = copy.deepcopy(canonical)

    # Fill blank fields from the remaining members in stable input order.
    for field in sorted({key for record in members for key in record}):
        if field in {
            "identity_key",
            "title",
            "primary_authors",
            "first_seen_date",
            "last_seen_date",
            "editor_status",
            "editor_note",
            DECISION_LOG_FIELD,
        }:
            continue
        values = [record[field] for record in members if field in record]
        current = projected.get(field)
        if any(isinstance(value, list | dict) for value in values):
            projected[field] = _merge_nonempty_values(values)
        elif not _nonblank(current) and any(_nonblank(value) for value in values):
            projected[field] = _merge_nonempty_values(values)

    # The decision logs combine by date, never by concatenation (issue #1053).
    decisions_log = merge_decision_logs(members)
    if decisions_log:
        projected[DECISION_LOG_FIELD] = decisions_log

    evidence = [
        evidence_record
        for record in members
        for evidence_record in (
            record.get("source_evidence", [])
            if isinstance(record.get("source_evidence", []), list)
            else [record["source_evidence"]]
        )
        if isinstance(evidence_record, dict)
    ]
    if evidence:
        projected["source_evidence"] = evidence

    # Generic field merging must not let a rendering win over a sourced
    # published title. Keep the title helper and its typed evidence together.
    title_metadata: dict[str, Any] = {}
    for member in members:
        title_metadata = merge_title_metadata(title_metadata, member)
    if title_metadata:
        projected.update(title_metadata)

    families: list[str] = []
    for record in members:
        raw_families = record.get("independent_signal_families", [])
        if isinstance(raw_families, list):
            for family in raw_families:
                family_text = _text(family)
                if family_text and family_text not in families:
                    families.append(family_text)
    evidence_families = [
        _text(evidence_record.get("source_family"))
        for evidence_record in projected.get("source_evidence", [])
        if isinstance(evidence_record, dict) and _text(evidence_record.get("source_family"))
    ]
    for family in evidence_families:
        if family not in families:
            families.append(family)
    if families:
        projected["independent_signal_families"] = families
    input_counts = [int(record.get("independent_signal_count", 0) or 0) for record in members]
    projected["independent_signal_count"] = max([*input_counts, len(families), 0])

    first_seen = _date_extreme(members, "first_seen_date", latest=False)
    last_seen = _date_extreme(members, "last_seen_date", latest=True)
    if first_seen is not None:
        projected["first_seen_date"] = first_seen
    if last_seen is not None:
        projected["last_seen_date"] = last_seen

    union_authors = _union_primary_authors(members, canonical_position)
    mention = {"title": canonical.get("title", ""), "primary_authors": union_authors}
    recomputed_key = normalized_identity_key(mention)
    projected["primary_authors"] = union_authors
    projected["identity_key"] = recomputed_key
    statuses, notes, decisions = _editor_metadata(members)
    if len(members) == 1:
        # Issue #661 P2: unrelated singletons keep their stored editor fields
        # verbatim, including empty strings versus missing keys. Only a real
        # identity merge may consolidate decisions.
        pass
    elif statuses:
        projected["editor_status"] = statuses[0]
    else:
        projected.pop("editor_status", None)
    if len(members) != 1:
        if notes:
            projected["editor_note"] = "\n\n".join(notes)
        else:
            projected.pop("editor_note", None)

    conflict_codes = _editor_conflicts(members)
    detail_members = []
    for index, record in zip(indices, members, strict=False):
        raw = _raw_mention(record)
        detail_members.append(
            {
                "history_index": index,
                "stored_key": _text(record.get("identity_key")),
                "recomputed_key": normalized_identity_key(raw),
                "title": record.get("title", ""),
                "candidate_id": _text(record.get("candidate_id")),
                "raw_authors": copy.deepcopy(record.get("primary_authors", [])),
                "editor_status": _text(record.get("editor_status")),
                "editor_note": _editor_note_value(record.get("editor_note")),
                "first_seen_date": record.get("first_seen_date", ""),
                "last_seen_date": record.get("last_seen_date", ""),
                "independent_signal_count": int(record.get("independent_signal_count", 0) or 0),
                "independent_signal_families": copy.deepcopy(record.get("independent_signal_families", [])),
                "source_evidence_count": len(
                    record.get("source_evidence", [])
                    if isinstance(record.get("source_evidence", []), list)
                    else ([record["source_evidence"]] if isinstance(record.get("source_evidence"), dict) else [])
                ),
            }
        )

    detail = {
        "title": canonical.get("title", ""),
        "canonical_identity_key": recomputed_key,
        "recomputed_key": recomputed_key,
        "canonical_stored_key": _text(canonical.get("identity_key")),
        "canonical_history_index": indices[canonical_position],
        "union_authors": copy.deepcopy(union_authors),
        "key_recomputed_from_union": True,
        "member_count": len(members),
        "members": detail_members,
        "editor_decisions": decisions,
        "preserved_editor_notes": copy.deepcopy(notes),
        "editor_status_conflicts": statuses if len(statuses) > 1 else [],
        "editor_note_conflicts": [
            "editor_note_without_status" for code in conflict_codes if code == "editor_note_without_status"
        ],
        "unsafe_editor_metadata": conflict_codes,
        "source_evidence_count_before": sum(member["source_evidence_count"] for member in detail_members),
        "source_evidence_count_projected": len(projected.get("source_evidence", [])),
        "independent_signal_count_projected": projected["independent_signal_count"],
        "independent_signal_families_projected": copy.deepcopy(projected.get("independent_signal_families", [])),
    }
    detail["future_reversal_warnings"] = [
        {
            "history_index": member["history_index"],
            "stored_key": member["stored_key"],
            "recomputed_key": member["recomputed_key"],
        }
        for member in detail_members
        if member["recomputed_key"] != recomputed_key
    ]
    return projected, detail


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _csv_value(row: dict[str, str], fields: tuple[str, ...]) -> str:
    for field in fields:
        if field in row:
            return _text(row[field])
    return ""


def _project_csv_rows(rows: list[dict[str, str]], mapping: dict[str, str]) -> list[dict[str, str]]:
    """Project and deterministically deduplicate editor rows without writing CSV."""
    projected: list[dict[str, str]] = []
    seen_candidate_ids: set[str] = set()
    for row in rows:
        result = dict(row)
        candidate_id = _csv_value(row, ("Candidate ID", "identity_key", "Identity key"))
        if not candidate_id:
            projected.append(result)
            continue
        projected_id = mapping.get(candidate_id, candidate_id)
        for field in ("Candidate ID", "identity_key", "Identity key"):
            if field in result:
                result[field] = projected_id
                break
        if projected_id in seen_candidate_ids:
            continue
        seen_candidate_ids.add(projected_id)
        projected.append(result)
    return projected


def _changed_csv_rows(rows: list[dict[str, str]], mapping: dict[str, str]) -> list[dict[str, str]]:
    """Return changed CSV rows using the same first-projected-row deduplication."""
    changed: list[dict[str, str]] = []
    seen_projected_ids: set[str] = set()
    for row in rows:
        stored_id = _csv_value(row, ("Candidate ID", "identity_key", "Identity key"))
        if not stored_id:
            continue
        projected_id = mapping.get(stored_id, stored_id)
        if projected_id in seen_projected_ids:
            continue
        seen_projected_ids.add(projected_id)
        if stored_id != projected_id:
            changed.append(
                {
                    "stored_candidate_id": stored_id,
                    "projected_candidate_id": projected_id,
                    "title": _csv_value(row, ("Title", "title")),
                    "author": _csv_value(row, ("Author", "author")),
                }
            )
    return changed


def _apply_durable_merge_approvals(
    report: dict[str, Any],
    records: list[dict[str, Any]],
    approvals: list[dict[str, Any]],
    existing_alias_table: dict[str, Any] | None,
) -> None:
    """Resolve approved durable-ID conflicts through recorded redirects.

    Mutates only the in-memory ``report`` (projected history, alias table,
    conflict lists, counts). Stored alias assignments are never overwritten:
    missing aliases are added pointing at the survivor, existing assignments
    to retired IDs are kept, and effective resolution follows the new redirect.
    Groups with unsafe editor metadata are never resolved; the approval is
    reported as blocked and the conflict remains.
    """
    cleaned: list[dict[str, Any]] = []
    seen_approvals: set[str] = set()
    for approval in approvals:
        item = validate_durable_merge_approval(approval)
        marker = _stable_value(item)
        if marker not in seen_approvals:
            seen_approvals.add(marker)
            cleaned.append(item)
    report["durable_merges"] = []
    alias_table = report.get("alias_table")
    if not isinstance(alias_table, dict):
        raise ValueError("migration report is missing its alias table")
    for approval in cleaned:
        canonical = approval["canonical_identity_key"]
        survivor = approval["survivor_candidate_id"]
        retired = list(approval["retired_candidate_ids"])
        plan = build_durable_id_merge_plan(records, alias_table, approval)
        entry: dict[str, Any] = {
            "canonical_identity_key": canonical,
            "survivor_candidate_id": survivor,
            "retired_candidate_ids": list(retired),
            "affected_candidate_ids": plan.get("affected_candidate_ids", []),
            "affected_identity_keys": plan.get("affected_identity_keys", []),
            "member_count": plan.get("member_count", 0),
            "editor_decisions": copy.deepcopy(plan.get("editor_decisions", [])),
            "preserved_editor_notes": copy.deepcopy(plan.get("preserved_editor_notes", [])),
            "editor_status_conflicts": copy.deepcopy(plan.get("editor_status_conflicts", [])),
            "unsafe_editor_metadata": copy.deepcopy(plan.get("unsafe_editor_metadata", [])),
            "guard": copy.deepcopy(plan.get("guard", {})),
            "survivor_rationale": plan.get("survivor_rationale", plan.get("reason", "")),
            "alias_effects": copy.deepcopy(plan.get("alias_effects", [])),
            "status": plan.get("status", "invalid"),
            "reason": plan.get("reason", ""),
        }
        if plan.get("status") == "already_applied":
            report["durable_merges"].append(entry)
            continue
        if plan.get("status") != "ready":
            report["durable_merges"].append(entry)
            continue
        group = next(
            (item for item in report.get("merge_groups", []) if item.get("canonical_identity_key") == canonical),
            None,
        )
        if group is None:
            entry["status"] = "stale"
            entry["reason"] = "no merge group matches the approved canonical key"
            report["durable_merges"].append(entry)
            continue
        if group.get("unsafe_editor_metadata"):
            entry["status"] = "blocked"
            entry["reason"] = f"unsafe editor metadata: {', '.join(group['unsafe_editor_metadata'])}"
            entry["unsafe_editor_metadata"] = copy.deepcopy(group["unsafe_editor_metadata"])
            report["durable_merges"].append(entry)
            continue
        member_ids = sorted({_text(member.get("candidate_id")) for member in group.get("members", [])})
        if member_ids != sorted({survivor, *retired}):
            entry["status"] = "stale"
            entry["reason"] = "approved IDs do not match the current merge group"
            report["durable_merges"].append(entry)
            continue
        conflict_index = next(
            (
                position
                for position, conflict in enumerate(report.get("alias_conflicts", []))
                if conflict.get("canonical_identity_key") == canonical
                and conflict.get("reason") == "multiple_durable_ids"
                and sorted(conflict.get("candidate_ids", [])) == sorted({survivor, *retired})
            ),
            None,
        )
        if conflict_index is None:
            entry["status"] = "stale"
            entry["reason"] = "no matching durable-ID conflict in the current plan"
            report["durable_merges"].append(entry)
            continue
        conflict = report["alias_conflicts"][conflict_index]
        new_additions: list[dict[str, str]] = []
        stale = False
        for alias in conflict.get("aliases", []):
            stored = stored_candidate_id_for_alias(alias_table, str(alias))
            if stored is None:
                alias_table.setdefault("aliases", {})[str(alias)] = survivor
                new_additions.append({"alias": str(alias), "candidate_id": survivor})
            elif effective_candidate_id(stored, alias_table) not in {survivor, *retired}:
                entry["status"] = "stale"
                entry["reason"] = f"alias {alias} now points elsewhere"
                report["durable_merges"].append(entry)
                stale = True
                break
        if stale:
            continue
        audit = {
            "canonical_identity_key": canonical,
            "survivor_candidate_id": survivor,
            "retired_candidate_ids": list(retired),
            "affected_aliases": copy.deepcopy(conflict.get("aliases", [])),
            "editor_decisions": copy.deepcopy(group.get("editor_decisions", [])),
            "guard": copy.deepcopy(plan.get("guard", {})),
            "survivor_rationale": entry["survivor_rationale"],
        }
        record_durable_id_merge(
            alias_table,
            canonical_identity_key=canonical,
            survivor_candidate_id=survivor,
            retired_candidate_ids=retired,
            audit_entry=audit,
        )
        for projected in report.get("projected_history", []):
            if str(projected.get("identity_key", "")) == canonical:
                projected["candidate_id"] = survivor
                survivor_meta = alias_table.setdefault("candidates", {}).setdefault(
                    survivor, {"candidate_id": survivor}
                )
                survivor_meta.setdefault("candidate_id", survivor)
                survivor_meta.setdefault("canonical_identity_key", canonical)
                survivor_stored = next(
                    (
                        str(member.get("stored_key", "")).strip()
                        for member in group.get("members", [])
                        if str(member.get("candidate_id", "")).strip() == survivor
                        and str(member.get("stored_key", "")).strip()
                    ),
                    canonical,
                )
                survivor_meta.setdefault("preferred_history_key", survivor_stored)
                if isinstance(projected.get("title"), str):
                    survivor_meta.setdefault("title", projected.get("title", ""))
                if isinstance(projected.get("primary_authors"), list):
                    survivor_meta.setdefault("primary_authors", copy.deepcopy(projected.get("primary_authors", [])))
        report["alias_conflicts"].pop(conflict_index)
        report["alias_conflicts"] = sorted(
            report["alias_conflicts"], key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False)
        )
        report["alias_conflict_count"] = len(report["alias_conflicts"])
        additions = report.setdefault("alias_additions", [])
        additions.extend(new_additions)
        additions.sort(key=lambda item: (item["alias"], item["candidate_id"]))
        report["alias_additions_count"] = len(additions)
        report["alias_table"] = copy.deepcopy(alias_table)
        entry["new_alias_additions"] = copy.deepcopy(new_additions)
        entry["reference_effects"] = {
            "merged_history_indices": [member.get("history_index") for member in group.get("members", [])],
            "projected_history_count": report.get("projected_history_count"),
            "retired_ids_resolve_to_survivor": list(retired),
        }
        report["durable_merges"].append(entry)
    report["durable_merge_count"] = len(report["durable_merges"])
    blocking_merges = [
        item for item in report["durable_merges"] if item.get("status") not in {"ready", "already_applied"}
    ]
    report["apply_blocked"] = (
        bool(report.get("unsafe_editor_metadata_conflicts"))
        or bool(report.get("alias_conflicts"))
        or bool(blocking_merges)
    )


def _singleton_changed_details(
    records: list[dict[str, Any]],
    groups: list[list[int]],
    details: list[dict[str, Any]],
    projected_history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return singleton projections differing from input, distinguishing missing vs null.

    Issue #661 P2: scope must reflect the final projection after alias seeding
    and approval processing (which can assign deterministic ``candidate_id``),
    and a missing key must not compare equal to an explicit ``null``.
    Unrekeyable legacy singletons are included: seeding can still assign them
    a deterministic ``candidate_id``, and that apply-time addition must appear
    in preview scope.
    """
    changed: list[dict[str, Any]] = []
    for projected, detail in zip(projected_history, details, strict=False):
        if detail.get("member_count") != 1:
            continue
        history_index = int(detail.get("canonical_history_index", -1))
        if history_index < 0 or history_index >= len(records):
            continue
        original = records[history_index]
        if _stable_value(original) == _stable_value(projected):
            # _stable_value already distinguishes missing vs explicit null at
            # the document level (``{}`` vs ``{"k":null}``), so equality here
            # means structurally preserved.
            continue
        union_keys = sorted(set(original) | set(projected), key=str)
        changed_fields: list[str] = []
        for key in union_keys:
            original_present = key in original
            projected_present = key in projected
            if original_present != projected_present:
                changed_fields.append(key)
            elif original_present and _stable_value(original[key]) != _stable_value(projected[key]):
                changed_fields.append(key)
        changed.append(
            {
                "history_index": history_index,
                "stored_key": _text(original.get("identity_key")),
                "projected_key": _text(projected.get("identity_key")),
                "changed_fields": changed_fields,
            }
        )
    changed.sort(key=lambda item: int(item["history_index"]))
    return changed


def build_candidate_history_migration_plan(
    candidate_history: list[dict[str, Any]],
    editor_csv_rows: list[dict[str, str]] | None = None,
    existing_alias_table: dict[str, Any] | None = None,
    durable_merge_approvals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a pure, deterministic migration plan from Candidate History.

    Without ``durable_merge_approvals`` the behavior is unchanged: a
    ``multiple_durable_ids`` alias conflict blocks apply. Each approval names
    exactly one pairwise merge; only the approved conflict is resolved through
    a recorded redirect, and every other conflict still blocks.
    """
    if not isinstance(candidate_history, list) or not all(isinstance(record, dict) for record in candidate_history):
        raise ValueError("candidate-history must be a JSON list of objects")

    records = [dict(record) for record in candidate_history]
    rekeyable = [index for index, record in enumerate(records) if _is_rekeyable(record)]
    unrekeyable = [index for index in range(len(records)) if index not in rekeyable]
    groups = _union_groups(records, rekeyable)

    projected_history: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    mapping: dict[str, str] = {}
    for group in groups:
        projected, detail = _merge_group(records, group)
        projected_history.append(projected)
        details.append(detail)
        for index in group:
            stored = _text(records[index].get("identity_key"))
            if stored:
                mapping[stored] = str(projected["identity_key"])
    for index in unrekeyable:
        record = copy.deepcopy(records[index])
        projected_history.append(record)
        detail = {
            "title": record.get("title", ""),
            "canonical_identity_key": _text(record.get("identity_key")),
            "recomputed_key": normalized_identity_key(_raw_mention(record)),
            "canonical_history_index": index,
            "member_count": 1,
            "members": [
                {
                    "history_index": index,
                    "stored_key": _text(record.get("identity_key")),
                    "recomputed_key": normalized_identity_key(_raw_mention(record)),
                    "title": record.get("title", ""),
                    "candidate_id": _text(record.get("candidate_id")),
                    "raw_authors": copy.deepcopy(record.get("primary_authors", [])),
                    "editor_status": _text(record.get("editor_status")),
                    "editor_note": _editor_note_value(record.get("editor_note")),
                    "first_seen_date": record.get("first_seen_date", ""),
                    "last_seen_date": record.get("last_seen_date", ""),
                    "independent_signal_count": int(record.get("independent_signal_count", 0) or 0),
                    "independent_signal_families": copy.deepcopy(record.get("independent_signal_families", [])),
                    "source_evidence_count": len(record.get("source_evidence", []))
                    if isinstance(record.get("source_evidence", []), list)
                    else 0,
                }
            ],
            "editor_decisions": (
                [{"status": _text(record.get("editor_status")), "note": _editor_note_value(record.get("editor_note"))}]
                if _text(record.get("editor_status")) or _text(record.get("editor_note"))
                else []
            ),
            "editor_status_conflicts": [],
            "editor_note_conflicts": [],
            "unsafe_editor_metadata": [],
            "source_evidence_count_before": len(record.get("source_evidence", []))
            if isinstance(record.get("source_evidence", []), list)
            else 0,
            "source_evidence_count_projected": len(record.get("source_evidence", []))
            if isinstance(record.get("source_evidence", []), list)
            else 0,
            "independent_signal_count_projected": int(record.get("independent_signal_count", 0) or 0),
            "independent_signal_families_projected": copy.deepcopy(record.get("independent_signal_families", [])),
            "unrekeyable": True,
        }
        details.append(detail)

    before_statuses = Counter(_text(record.get("editor_status")) for record in records)
    after_statuses = Counter(_text(record.get("editor_status")) for record in projected_history)
    before_notes = sum(bool(_text(record.get("editor_note"))) for record in records)
    after_notes = sum(bool(_text(record.get("editor_note"))) for record in projected_history)
    unsafe_groups = [detail for detail in details if detail["unsafe_editor_metadata"]]
    signal_checks = [
        {
            "canonical_identity_key": detail["canonical_identity_key"],
            "count_preserved": detail["independent_signal_count_projected"]
            >= max((member["independent_signal_count"] for member in detail["members"]), default=0),
            "families_preserved": set(detail["independent_signal_families_projected"]).issuperset(
                {
                    _text(family)
                    for member in detail["members"]
                    for family in member["independent_signal_families"]
                    if _text(family)
                }
            ),
        }
        for detail in details
    ]
    report: dict[str, Any] = {
        "migration": "candidate-history-identity-rekey",
        "history_entries_before": len(records),
        "history_entries_projected": len(projected_history),
        "projected_history_count": len(projected_history),
        "distinct_stored_keys": len({_text(record.get("identity_key")) for record in records}),
        "distinct_blindly_recomputed_keys": len({normalized_identity_key(_raw_mention(record)) for record in records}),
        "rekey_only_collapse": len(records)
        - len({normalized_identity_key(_raw_mention(record)) for record in records}),
        "distinct_safe_recomputed_keys": len(
            {normalized_identity_key(_raw_mention(records[index])) for index in rekeyable}
        ),
        "safe_exact_key_collapse": len(rekeyable)
        - len({normalized_identity_key(_raw_mention(records[index])) for index in rekeyable}),
        "rekeyable_count": len(rekeyable),
        "unsafe_rekeyable_count": sum(
            detail["member_count"]
            for detail in details
            if not detail.get("unrekeyable") and detail["unsafe_editor_metadata"]
        ),
        "unrekeyable_count": len(unrekeyable),
        "unrekeyable_records": [
            {
                "history_index": index,
                "stored_key": _text(records[index].get("identity_key")),
                "recomputed_key": normalized_identity_key(_raw_mention(records[index])),
                "title": records[index].get("title", ""),
                "raw_authors": copy.deepcopy(records[index].get("primary_authors", [])),
            }
            for index in unrekeyable
        ],
        "merge_group_count": sum(detail["member_count"] > 1 for detail in details),
        "merge_groups": [detail for detail in details if detail["member_count"] > 1],
        "identity_groups": details,
        "editor_status_counts_before": dict(sorted(before_statuses.items())),
        "editor_status_counts_projected": dict(sorted(after_statuses.items())),
        "editor_note_count_before": before_notes,
        "editor_note_count_projected": after_notes,
        "editor_status_conflict_count": sum(
            "multiple_editor_statuses" in detail["unsafe_editor_metadata"] for detail in details
        ),
        "editor_note_conflict_count": sum(
            "editor_note_without_status" in detail["unsafe_editor_metadata"] for detail in details
        ),
        "editor_note_without_status_explanation": (
            "A note without a status cannot be safely associated with an editor decision after records merge; "
            "it is a hard apply conflict."
        ),
        "unsafe_editor_metadata_conflicts": unsafe_groups,
        "editor_decisions": [decision for detail in details for decision in detail.get("editor_decisions", [])],
        "apply_blocked": bool(unsafe_groups),
        "independent_signal_preservation_checks": signal_checks,
        "independent_signal_preserved": all(
            check["count_preserved"] and check["families_preserved"] for check in signal_checks
        ),
        "source_evidence_count_before": sum(detail["source_evidence_count_before"] for detail in details),
        "source_evidence_count_projected": sum(detail["source_evidence_count_projected"] for detail in details),
        "future_reversal_warning_count": sum(len(detail.get("future_reversal_warnings", [])) for detail in details),
        "future_reversal_warnings": [
            {
                "title": detail["title"],
                "canonical_identity_key": detail["canonical_identity_key"],
                "members": copy.deepcopy(detail["future_reversal_warnings"]),
            }
            for detail in details
            if detail.get("future_reversal_warnings")
        ],
        "projected_history": projected_history,
        "projected_distinct_key_count": len({str(record.get("identity_key", "")) for record in projected_history}),
        # Issue #661 P2: placeholder, recomputed after seeding and approvals
        # so preview scope matches the bytes apply will write.
        "singleton_changed_count": 0,
        "singleton_changed_details": [],
    }
    # Durable IDs are assigned once from the migration grouping above.  The
    # alias table also retains every member key that can recur in a future run.
    # NOTE: seeding mutates report["projected_history"] in place, assigning a
    # deterministic candidate_id where missing.
    seed_alias_table_from_migration(report, existing_alias_table)
    if durable_merge_approvals:
        _apply_durable_merge_approvals(report, records, durable_merge_approvals, existing_alias_table)
    # Issue #661 P2: calculate singleton scope after alias seeding and approval
    # processing, so preview reflects candidate_id assignments and durable-merge
    # candidate_id rewrites. Singleton groups have no identity merge to justify
    # other content changes; any remaining difference is an unrelated-record
    # mutation that must stay visible before apply.
    final_singleton_details = _singleton_changed_details(records, groups, details, report["projected_history"])
    report["singleton_changed_details"] = final_singleton_details
    report["singleton_changed_count"] = len(final_singleton_details)
    if editor_csv_rows is not None:
        projected_csv_rows = _project_csv_rows(editor_csv_rows, mapping)
        report["editor_csv_rows_before"] = copy.deepcopy(editor_csv_rows)
        report["editor_csv_rows_projected"] = projected_csv_rows
        report["editor_csv_rows_before_count"] = len(editor_csv_rows)
        report["editor_csv_rows_projected_count"] = len(projected_csv_rows)
        report["editor_csv_changed_rows"] = _changed_csv_rows(editor_csv_rows, mapping)
    return report


def _markdown_cell(value: Any) -> str:
    """Escape a value for one Markdown table cell."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r\n", "<br>")
        .replace("\n", "<br>")
        .replace("\r", "<br>")
    )


def render_candidate_history_migration_report(report: dict[str, Any]) -> str:
    """Render a deterministic Markdown report without changing state."""
    summary_keys = (
        "history_entries_before",
        "distinct_stored_keys",
        "distinct_blindly_recomputed_keys",
        "rekeyable_count",
        "unrekeyable_count",
        "rekey_only_collapse",
        "distinct_safe_recomputed_keys",
        "safe_exact_key_collapse",
        "merge_group_count",
        "projected_history_count",
        "projected_distinct_key_count",
        "singleton_changed_count",
        "editor_status_counts_before",
        "editor_note_count_before",
        "editor_status_conflict_count",
        "editor_note_conflict_count",
        "independent_signal_preserved",
    )
    lines = ["# Candidate History migration report", "", "## Summary", ""]
    for key in summary_keys:
        lines.append(f"- `{key}`: `{_stable_value(report.get(key))}`")
    lines.extend(
        [
            f"- `alias_additions`: `{report.get('alias_additions_count', len(report.get('alias_additions', [])))}`",
            f"- `alias_reused`: `{report.get('alias_reused_count', len(report.get('alias_reused', [])))}`",
            f"- `alias_conflict_count`: `{report.get('alias_conflict_count', 0)}`",
        ]
    )
    lines.append(f"- `editor_note_without_status`: `{report.get('editor_note_without_status_explanation', '')}`")
    lines.extend(["", "## Durable alias seeding", ""])
    conflicts = report.get("alias_conflicts", [])
    lines.append(f"- Alias additions: `{report.get('alias_additions_count', len(report.get('alias_additions', [])))}`.")
    lines.append(f"- Reused aliases: `{report.get('alias_reused_count', len(report.get('alias_reused', [])))}`.")
    lines.append(f"- Conflicts: `{report.get('alias_conflict_count', len(conflicts))}`.")
    if conflicts:
        lines.append("- Conflict details:")
        for conflict in conflicts:
            lines.append(
                f"  - Canonical key `{conflict.get('canonical_identity_key', '')}`; "
                f"aliases `{_stable_value(conflict.get('aliases', []))}`; "
                f"candidate IDs `{_stable_value(conflict.get('candidate_ids', []))}`; "
                f"reason `{conflict.get('reason', '')}`."
            )
    else:
        lines.append("- Conflict details: None.")
    lines.extend(["", "## Unrekeyable legacy records", ""])
    for record in report.get("unrekeyable_records", []):
        lines.append(
            f"- Index `{record['history_index']}`, stored key `{record['stored_key']}`, "
            f"raw title `{record['title']}`, raw authors `{_stable_value(record['raw_authors'])}`."
        )
    if not report.get("unrekeyable_records"):
        lines.append("- None.")
    lines.extend(["", "## Singleton content changes (unrelated-record scope)", ""])
    singleton_changes = report.get("singleton_changed_details", [])
    lines.append(f"- Singleton records with projected content changes: `{len(singleton_changes)}`.")
    if singleton_changes:
        lines.append("- Changed singletons (review before apply):")
        for change in singleton_changes:
            lines.append(
                f"  - Index `{change.get('history_index', '')}`, stored key `{change.get('stored_key', '')}` "
                f"-> `{change.get('projected_key', '')}`; "
                f"changed fields `{_stable_value(change.get('changed_fields', []))}`."
            )
    else:
        lines.append("- Changed singletons: None. Unrelated records are structurally preserved.")
    lines.extend(["", "## Merge groups", ""])
    groups = report.get("merge_groups", [])
    if not groups:
        lines.append("- None.")
    for number, group in enumerate(groups, start=1):
        lines.extend(
            [
                f"### Group {number}: {group['title']}",
                f"- Canonical source history index: `{group['canonical_history_index']}`.",
                f"- Union authors: `{_stable_value(group['union_authors'])}`.",
                f"- Identity key: `{group['canonical_identity_key']}` (recomputed from the union authors).",
                f"- Source evidence: `{group['source_evidence_count_before']}` before, "
                f"`{group['source_evidence_count_projected']}` projected.",
                f"- Preserved editor notes: `{_stable_value(group['preserved_editor_notes'])}`.",
                f"- Unsafe editor metadata: `{_stable_value(group['unsafe_editor_metadata'])}`",
                f"- Future exact-key reversal warnings: `{_stable_value(group['future_reversal_warnings'])}`.",
            ]
        )
        for member in group["members"]:
            lines.append(
                f"- Member `{member['stored_key']}` -> `{member['recomputed_key']}`; "
                f"authors `{_stable_value(member['raw_authors'])}`; status `{member['editor_status']}`; "
                f"note `{member['editor_note']}`; first `{member['first_seen_date']}`; "
                f"last `{member['last_seen_date']}`; signals `{member['independent_signal_count']}`; "
                f"families `{_stable_value(member['independent_signal_families'])}`; "
                f"External Signal/source evidence `{member['source_evidence_count']}`."
            )
    if "editor_csv_rows_before" in report:
        lines.extend(["", "## Editor CSV projection", ""])
        lines.append(f"- Rows before: `{report['editor_csv_rows_before_count']}`")
        lines.append(f"- Projected rows after: `{report['editor_csv_rows_projected_count']}`")
        lines.extend(
            [
                "",
                "| Stored Candidate ID | Projected Candidate ID | Title | Author |",
                "| --- | --- | --- | --- |",
            ]
        )
        changed_rows = report.get("editor_csv_changed_rows", [])
        if changed_rows:
            for row in changed_rows:
                lines.append(
                    "| "
                    + " | ".join(
                        _markdown_cell(row[field])
                        for field in ("stored_candidate_id", "projected_candidate_id", "title", "author")
                    )
                    + " |"
                )
        else:
            lines.append("| None | None | None | None |")
    if "durable_merges" in report:
        lines.extend(["", "## Approved durable-ID merges", ""])
        merges = report["durable_merges"]
        if not merges:
            lines.append("- None.")
        for merge in merges:
            lines.extend(
                [
                    f"- Canonical key `{merge.get('canonical_identity_key', '')}`; "
                    f"survivor `{merge.get('survivor_candidate_id', '')}`; "
                    f"retired `{_stable_value(merge.get('retired_candidate_ids', []))}`; "
                    f"status `{merge.get('status', '')}`; reason `{merge.get('reason', '')}`.",
                    f"  - Affected IDs `{_stable_value(merge.get('affected_candidate_ids', []))}`; "
                    f"keys `{_stable_value(merge.get('affected_identity_keys', []))}`.",
                    f"  - Preserved editor decisions `{_stable_value(merge.get('editor_decisions', []))}`; "
                    f"conflicts `{_stable_value(merge.get('editor_status_conflicts', []))}`.",
                    f"  - Guard `{_stable_value(merge.get('guard', {}))}`; rationale `{merge.get('survivor_rationale', '')}`.",
                ]
            )
    return "\n".join(lines) + "\n"


def load_durable_merge_approvals(paths: list[Path] | None) -> list[dict[str, Any]]:
    """Load and validate durable-merge approval files without touching state."""
    approvals: list[dict[str, Any]] = []
    for path in paths or []:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"durable merge approval not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid durable merge approval JSON: {path.name}") from exc
        approvals.append(validate_durable_merge_approval(value))
    return approvals


def migrate_candidate_history(
    state_dir: Path,
    *,
    report_path: Path | None = None,
    editor_csv_path: Path | None = None,
    apply: bool = False,
    durable_merge_approvals: list[dict[str, Any]] | None = None,
    state_store: StateStore | None = None,
) -> tuple[dict[str, Any], bool]:
    """Plan and optionally apply the migration.

    Returns the report and whether Candidate History or the alias table
    changed. A blocked apply raises ``ValueError`` before any state write.
    Approved durable-ID merges reuse this same write path: without an
    approval a ``multiple_durable_ids`` conflict still blocks.
    """
    store = open_state_store(state_dir) if state_store is None else state_store
    changed = False
    # History and the alias table are read, planned from and written in one
    # store transaction, so an interrupted apply leaves both as they were.
    with store.transaction():
        history = store.read(Collection.CANDIDATE_HISTORY)
        # The store reads a missing history as an empty one; both are refused.
        if not isinstance(history, list) or not history:
            raise ValueError("candidate-history must be a JSON list")
        rows = _csv_rows(editor_csv_path) if editor_csv_path is not None else None
        existing_aliases = store.read(Collection.CANDIDATE_IDENTITY_ALIASES)
        report = build_candidate_history_migration_plan(history, rows, existing_aliases, durable_merge_approvals)
        markdown = render_candidate_history_migration_report(report)
        if report_path is not None:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(markdown, encoding="utf-8")
        if report["apply_blocked"] and apply:
            raise ValueError("apply blocked by unsafe editor metadata conflicts")
        if apply:
            for collection, value in (
                (Collection.CANDIDATE_IDENTITY_ALIASES, report["alias_table"]),
                (Collection.CANDIDATE_HISTORY, report["projected_history"]),
            ):
                before = store.read(collection)
                store.replace(collection, value)
                # Read back in the store's canonical form and order.
                if store.read(collection) != before:
                    changed = True
    return report, changed


def emit_durable_merge_approval(
    candidate_history: list[dict[str, Any]],
    existing_alias_table: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build an explicit approval template for the single pending durable-ID merge.

    The template binds the exact reviewed records, so the operator reviews the
    same editor decisions the apply step will require. Exactly one
    ``multiple_durable_ids`` conflict must be pending, its identity guard must
    pass, and its editor metadata must be safe; otherwise no template is
    produced and ``ValueError`` explains why.
    """
    if not isinstance(candidate_history, list) or not all(isinstance(record, dict) for record in candidate_history):
        raise ValueError("candidate-history must be a JSON list of objects")
    plan = build_candidate_history_migration_plan(candidate_history, None, existing_alias_table)
    conflicts = [
        conflict for conflict in plan.get("alias_conflicts", []) if conflict.get("reason") == "multiple_durable_ids"
    ]
    if not conflicts:
        raise ValueError("no pending durable-ID merge to approve")
    if len(conflicts) > 1:
        raise ValueError("more than one pending durable-ID merge; approve exactly one at a time")
    conflict = conflicts[0]
    canonical = str(conflict.get("canonical_identity_key", ""))
    wanted = {str(item) for item in conflict.get("candidate_ids", [])}
    members = [record for record in candidate_history if _text(record.get("candidate_id")) in wanted]
    guard = _durable_merge_guard(members)
    if not guard["passed"]:
        raise ValueError("cannot emit a safe approval: identity guard failed")
    editor_conflicts = _editor_conflicts(members)
    if editor_conflicts:
        raise ValueError(f"cannot emit a safe approval: {', '.join(editor_conflicts)}")
    survivor, retired, _rationale = _durable_merge_survivor(members, canonical)
    bindings = [_live_member_binding(record) for record in members]
    if any(item is None for item in bindings):
        raise ValueError("cannot emit a safe approval: members cannot be bound exactly")
    approval = validate_durable_merge_approval(
        {
            "canonical_identity_key": canonical,
            "survivor_candidate_id": survivor,
            "retired_candidate_ids": retired,
            "expected_members": [item for item in bindings if item is not None],
        }
    )
    emitted_plan = build_durable_id_merge_plan(candidate_history, existing_alias_table, approval)
    if emitted_plan.get("status") != "ready":
        raise ValueError(f"cannot emit a safe approval: {emitted_plan.get('reason', 'plan is not ready')}")
    return approval
