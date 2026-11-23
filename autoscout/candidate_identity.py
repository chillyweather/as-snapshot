"""Durable, deterministic Candidate identity aliases (Issue #443)."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoscout.author_admission import admitted_author_people
from autoscout.candidate_schema import CandidateIsbn
from autoscout.identity import mentions_are_identity_match
from autoscout.identity import normalized_identity_key

ALIAS_TABLE_VERSION = 1
ALIAS_TABLE_VERSION_WITH_MERGES = 2
SUPPORTED_ALIAS_TABLE_VERSIONS = (ALIAS_TABLE_VERSION, ALIAS_TABLE_VERSION_WITH_MERGES)


def empty_alias_table() -> dict[str, Any]:
    return {"version": ALIAS_TABLE_VERSION, "aliases": {}, "candidates": {}}


def _clean_redirects(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("candidate identity redirects must be an object")
    clean: dict[str, str] = {}
    for retired, survivor in value.items():
        if not isinstance(retired, str) or not retired.strip():
            raise ValueError("candidate identity redirect must map nonblank IDs")
        if not isinstance(survivor, str) or not survivor.strip():
            raise ValueError("candidate identity redirect must map to a nonblank survivor ID")
        if retired == survivor:
            raise ValueError("candidate identity redirect cannot point at itself")
        clean[retired] = survivor
    for retired in clean:
        chain = {retired}
        cursor: str | None = clean[retired]
        while cursor is not None and cursor in clean:
            if cursor in chain:
                raise ValueError("candidate identity redirects must not contain a cycle")
            chain.add(cursor)
            cursor = clean.get(cursor)
    return clean


def _clean_durable_merges(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("candidate identity durable merges must be a list")
    clean: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("candidate identity durable merge must be an object")
        canonical = entry.get("canonical_identity_key", "")
        survivor = entry.get("survivor_candidate_id", "")
        retired = entry.get("retired_candidate_ids", [])
        if not isinstance(canonical, str) or not canonical.strip():
            raise ValueError("candidate identity durable merge needs a canonical identity key")
        if not isinstance(survivor, str) or not survivor.strip():
            raise ValueError("candidate identity durable merge needs a survivor candidate ID")
        if (
            not isinstance(retired, list)
            or not retired
            or not all(isinstance(item, str) and item.strip() for item in retired)
        ):
            raise ValueError("candidate identity durable merge needs retired candidate IDs")
        if survivor in retired:
            raise ValueError("candidate identity durable merge survivor must not be retired")
        clean.append(copy.deepcopy(entry))
    return clean


def validate_alias_table(value: Any) -> dict[str, Any]:
    """Validate and copy the persisted alias table.

    Invalid persisted state is an error. Silently replacing it with an empty
    table could attach new editor decisions to the wrong work.
    """
    if value is None:
        return empty_alias_table()
    if not isinstance(value, dict):
        raise ValueError("candidate identity aliases must be an object")
    version = value.get("version", ALIAS_TABLE_VERSION)
    if version not in SUPPORTED_ALIAS_TABLE_VERSIONS:
        raise ValueError(f"unsupported candidate identity alias version: {version}")
    aliases = value.get("aliases", {})
    candidates = value.get("candidates", {})
    if not isinstance(aliases, dict) or not isinstance(candidates, dict):
        raise ValueError("candidate identity aliases and candidates must be objects")
    clean_aliases: dict[str, str] = {}
    for alias, candidate_id in aliases.items():
        if not isinstance(alias, str) or not alias.strip() or not isinstance(candidate_id, str) or not candidate_id:
            raise ValueError("candidate identity aliases must map nonblank strings to candidate IDs")
        clean_aliases[alias] = candidate_id
    clean_candidates: dict[str, dict[str, Any]] = {}
    for candidate_id, metadata in candidates.items():
        if not isinstance(candidate_id, str) or not candidate_id or not isinstance(metadata, dict):
            raise ValueError("candidate identity candidate metadata is invalid")
        clean_candidates[candidate_id] = copy.deepcopy(metadata)
        clean_candidates[candidate_id].setdefault("candidate_id", candidate_id)
        if str(clean_candidates[candidate_id]["candidate_id"]) != candidate_id:
            raise ValueError("candidate identity candidate metadata has a mismatched ID")
    if version == ALIAS_TABLE_VERSION_WITH_MERGES:
        redirects = _clean_redirects(value.get("redirects", {}))
        merges = _clean_durable_merges(value.get("durable_merges", []))
        return {
            "version": ALIAS_TABLE_VERSION_WITH_MERGES,
            "aliases": clean_aliases,
            "candidates": clean_candidates,
            "redirects": redirects,
            "durable_merges": merges,
        }
    if "redirects" in value or "durable_merges" in value:
        raise ValueError("candidate identity redirects require alias table version 2")
    return {"version": ALIAS_TABLE_VERSION, "aliases": clean_aliases, "candidates": clean_candidates}


def ensure_alias_table(value: dict[str, Any] | None) -> dict[str, Any]:
    """Return one mutable, validated table for a complete operation.

    ``validate_alias_table`` intentionally returns a defensive copy for file
    boundaries. Pipeline stages must instead share one table so aliases added
    by an early stage reach later stages and state persistence.
    """
    if value is None:
        return empty_alias_table()
    validated = validate_alias_table(value)
    value.clear()
    value.update(validated)
    return value


def load_alias_table(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty_alias_table()
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid candidate identity alias JSON: {path.name}") from exc
    return validate_alias_table(value)


def save_alias_table(path: Path, table: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(validate_alias_table(table), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def candidate_id_for(identity_key: str) -> str:
    """Return the stable ID for a first assignment."""
    return "candidate-" + hashlib.sha256(identity_key.encode("utf-8")).hexdigest()[:24]


SHORT_CANDIDATE_ID_MIN_LENGTH = 8


def short_candidate_ids(candidate_ids: Iterable[str]) -> dict[str, str]:
    """Return one short, copyable form per durable Candidate ID (Issue #851).

    The short form is the tail of the hex digest, at least eight characters.
    Every ID gets the same length, and the length grows until no two of the
    given IDs share a tail, so a short ID names exactly one of them.
    """
    digests = {candidate_id: candidate_id.removeprefix("candidate-") for candidate_id in candidate_ids if candidate_id}
    length = SHORT_CANDIDATE_ID_MIN_LENGTH
    longest = max((len(digest) for digest in digests.values()), default=length)
    while length < longest and len({digest[-length:] for digest in digests.values()}) < len(digests):
        length += 1
    return {candidate_id: digest[-length:] for candidate_id, digest in digests.items()}


def _mention(record: dict[str, Any]) -> dict[str, Any]:
    authors = record.get("primary_authors", [])
    mention: dict[str, Any] = {
        "title": record.get("title", ""),
        "primary_authors": authors if isinstance(authors, list) else [],
    }
    # The ISBN value and its identity-use state travel as one pair.
    CandidateIsbn.from_record(record).apply_to(mention)
    return mention


def _record_keys(record: dict[str, Any]) -> list[str]:
    """Return stored and safely recomputed keys without inventing authors."""
    keys: list[str] = []
    stored = str(record.get("identity_key", "")).strip()
    if stored:
        keys.append(stored)
    authors = record.get("primary_authors")
    has_authors = isinstance(authors, list) and any(str(author).strip() for author in authors)
    if str(record.get("title", "")).strip() and has_authors:
        computed = normalized_identity_key(_mention(record))
        if computed and computed not in keys:
            keys.append(computed)
    return keys


def _admitted_mention(record: dict[str, Any]) -> dict[str, Any] | None:
    """Return a record's mention with its stored authors admitted under today's rules.

    Admission rules can change after a record was stored: a stored
    ``Joseph Grenny, Kerry Patterson, Ron McMillan, and others`` is now
    admitted as three people (Issue #854). The stored record is never
    rewritten; this mention only lets a later sighting find the record again.
    None means admission leaves the record unchanged or it stores no title.
    """
    authors = record.get("primary_authors")
    if not str(record.get("title", "")).strip() or not isinstance(authors, list):
        return None
    # A stored ``["MD"]`` admits nobody today, like the title-only sighting.
    people = [person for author in authors for person in admitted_author_people(author)]
    if people == authors:
        return None
    return {**_mention(record), "primary_authors": people}


@dataclass(frozen=True, eq=False)
class KeyedHistory:
    """Candidate History records paired with their identity keys (Issue #719).

    Resolving one candidate compares it with every history record, so a stage
    that resolves N candidates against M records derived the same M sets of
    keys N times. A stage builds one ``KeyedHistory`` before its loop and
    passes it to every resolution, so each record's keys are derived once.
    The keys read only a record's stored key, title, authors, and ISBN, which
    resolution never changes. Only records with a stored identity key are
    kept, the same records history matching has always considered.
    """

    records: tuple[dict[str, Any], ...]
    _keys_by_record: dict[int, tuple[str, ...]]
    _admitted_by_record: dict[int, tuple[dict[str, Any], str] | None]

    @classmethod
    def of(cls, history: Sequence[Any]) -> KeyedHistory:
        usable = tuple(
            record for record in history if isinstance(record, dict) and str(record.get("identity_key", "")).strip()
        )
        # Keyed by object identity: ``records`` holds every record, so no
        # identity is reused while this history exists.
        return cls(
            usable,
            {id(record): tuple(_record_keys(record)) for record in usable},
            {id(record): _admitted_entry(record) for record in usable},
        )

    def keys(self, record: dict[str, Any]) -> tuple[str, ...]:
        """Return the identity keys of one of ``records``."""
        return self._keys_by_record[id(record)]

    def admitted_key(self, record: dict[str, Any]) -> str:
        """Return the key a fresh sighting gets when admission changed the record, or ``""``."""
        entry = self._admitted_by_record[id(record)]
        return entry[1] if entry else ""

    def admitted_mention(self, record: dict[str, Any]) -> dict[str, Any] | None:
        """Return the record's mention under today's admission when it changed, or None."""
        entry = self._admitted_by_record[id(record)]
        return entry[0] if entry else None


def _admitted_entry(record: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    mention = _admitted_mention(record)
    return None if mention is None else (mention, normalized_identity_key(mention))


def _metadata_mention(metadata: dict[str, Any]) -> dict[str, Any] | None:
    if "title" not in metadata or "primary_authors" not in metadata:
        return None
    authors = metadata.get("primary_authors")
    mention: dict[str, Any] = {
        "title": metadata.get("title", ""),
        "primary_authors": authors if isinstance(authors, list) else [],
    }
    # The ISBN value and its identity-use state travel as one pair.
    CandidateIsbn.from_record(metadata).apply_to(mention)
    return mention


def assign_alias(table: dict[str, Any], alias: str, candidate_id: str) -> dict[str, str] | None:
    aliases = table.setdefault("aliases", {})
    current = aliases.get(alias)
    if current and effective_candidate_id(str(current), table) != effective_candidate_id(candidate_id, table):
        return {"alias": alias, "existing_candidate_id": str(current), "candidate_id": candidate_id}
    aliases.setdefault(alias, candidate_id)
    table.setdefault("candidates", {}).setdefault(candidate_id, {"candidate_id": candidate_id})
    return None


def assign_aliases(table: dict[str, Any], aliases: list[str], candidate_id: str) -> dict[str, Any] | None:
    """Add aliases atomically, returning a conflict before any mutation.

    Stored assignments that redirect to one surviving ID agree. Comparisons
    use effective IDs so an approved merge never raises a fresh conflict,
    while stored alias ownership is never overwritten here.
    """
    unique_aliases = sorted({alias.strip() for alias in aliases if alias and alias.strip()})
    conflicts = [
        {
            "alias": alias,
            "existing_candidate_id": str(table.get("aliases", {}).get(alias)),
            "candidate_id": candidate_id,
        }
        for alias in unique_aliases
        if alias in table.get("aliases", {})
        and effective_candidate_id(str(table["aliases"][alias]), table) != effective_candidate_id(candidate_id, table)
    ]
    if conflicts:
        return {"conflicts": conflicts}
    for alias in unique_aliases:
        assign_alias(table, alias, candidate_id)
    return None


def record_durable_id_merge(
    table: dict[str, Any],
    *,
    canonical_identity_key: str,
    survivor_candidate_id: str,
    retired_candidate_ids: list[str],
    audit_entry: dict[str, Any] | None = None,
) -> None:
    """Record an approved durable-ID merge without rewriting stored aliases.

    Stored ``aliases`` entries keep pointing at the retired IDs they were
    assigned to. Effective resolution follows the new ``redirects`` entries to
    the survivor. Retired candidate metadata is retained and annotated with
    ``redirect_to`` instead of being deleted. The audit entry is appended to
    ``durable_merges`` once; repeating the same merge changes nothing.
    """
    canonical = canonical_identity_key.strip()
    survivor = survivor_candidate_id.strip()
    retired = sorted({item.strip() for item in retired_candidate_ids if item.strip()})
    if not canonical:
        raise ValueError("durable merge needs a canonical identity key")
    if not survivor:
        raise ValueError("durable merge needs a survivor candidate ID")
    if not retired:
        raise ValueError("durable merge needs retired candidate IDs")
    if survivor in retired:
        raise ValueError("durable merge survivor must not be retired")
    validated = validate_alias_table(table)
    current_redirects = validated.get("redirects", {})
    if not isinstance(current_redirects, dict):
        raise ValueError("candidate identity redirects must be an object")
    if survivor in current_redirects:
        raise ValueError(f"durable merge survivor {survivor} is itself retired")
    projected = dict(current_redirects)
    for retired_id in retired:
        current = projected.get(retired_id)
        if current is not None and current != survivor:
            raise ValueError(f"retired candidate ID {retired_id} already redirects elsewhere")
        projected[retired_id] = survivor
    for start in projected:
        chain = {start}
        cursor: str | None = projected[start]
        while cursor is not None and cursor in projected:
            if cursor in chain:
                raise ValueError("durable merge would create a redirect cycle")
            chain.add(cursor)
            cursor = projected.get(cursor)
    table.clear()
    table.update(validated)
    table["version"] = ALIAS_TABLE_VERSION_WITH_MERGES
    redirects = table.setdefault("redirects", {})
    if not isinstance(redirects, dict):
        raise ValueError("candidate identity redirects must be an object")
    for retired_id in retired:
        redirects[retired_id] = survivor
    candidates = table.setdefault("candidates", {})
    survivor_meta = candidates.setdefault(survivor, {"candidate_id": survivor})
    if not isinstance(survivor_meta, dict):
        raise ValueError("candidate identity candidate metadata is invalid")
    survivor_meta.setdefault("candidate_id", survivor)
    survivor_target = survivor_meta.get("redirect_to")
    if survivor_target is not None and str(survivor_target) != survivor:
        raise ValueError(f"durable merge survivor {survivor} is itself retired")
    for retired_id in retired:
        meta = candidates.setdefault(retired_id, {"candidate_id": retired_id})
        if not isinstance(meta, dict):
            raise ValueError("candidate identity candidate metadata is invalid")
        meta.setdefault("candidate_id", retired_id)
        current_target = meta.get("redirect_to")
        if current_target is not None and str(current_target) != survivor:
            raise ValueError(f"retired candidate ID {retired_id} already redirects elsewhere")
        meta["redirect_to"] = survivor
        meta.setdefault("canonical_identity_key", canonical)
    merges = table.setdefault("durable_merges", [])
    if not isinstance(merges, list):
        raise ValueError("candidate identity durable merges must be a list")
    entry = copy.deepcopy(audit_entry) if isinstance(audit_entry, dict) else {}
    entry["canonical_identity_key"] = canonical
    entry["survivor_candidate_id"] = survivor
    entry["retired_candidate_ids"] = list(retired)
    marker = json.dumps(
        {
            "canonical_identity_key": entry["canonical_identity_key"],
            "survivor_candidate_id": entry["survivor_candidate_id"],
            "retired_candidate_ids": entry["retired_candidate_ids"],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    for existing in merges:
        if not isinstance(existing, dict):
            continue
        existing_marker = json.dumps(
            {
                "canonical_identity_key": existing.get("canonical_identity_key", ""),
                "survivor_candidate_id": existing.get("survivor_candidate_id", ""),
                "retired_candidate_ids": sorted(existing.get("retired_candidate_ids", [])),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        if existing_marker == marker:
            return
    merges.append(entry)


def _candidate_metadata(table: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    metadata = table.setdefault("candidates", {}).get(candidate_id, {})
    return metadata if isinstance(metadata, dict) else {}


def stored_candidate_id_for_alias(table: dict[str, Any], alias: str) -> str | None:
    """Return the stored alias assignment without following a recorded redirect."""
    aliases = table.get("aliases", {})
    if not isinstance(aliases, dict):
        return None
    stored = aliases.get(alias)
    return str(stored) if isinstance(stored, str) and stored else None


def effective_candidate_id(candidate_id: str, table: dict[str, Any]) -> str:
    """Follow recorded durable-ID redirects to the surviving ID.

    Stored alias assignments never change. Effective resolution follows the
    append-only redirect chain so every alias of a retired ID resolves to the
    survivor. A missing redirect entry resolves to the input unchanged.
    """
    redirects = table.get("redirects", {})
    if not isinstance(redirects, dict):
        return candidate_id
    current = candidate_id
    seen = {current}
    while isinstance(current, str) and current in redirects:
        nxt = redirects[current]
        if not isinstance(nxt, str) or not nxt:
            break
        if nxt in seen:
            raise ValueError("candidate identity redirects must not contain a cycle")
        seen.add(nxt)
        current = nxt
    return current


def has_conflicting_aliases(record: dict[str, Any], table: dict[str, Any]) -> bool:
    """Return whether aliases or an explicit ID disagree for one record.

    Stored assignments that redirect to one surviving ID agree. Only
    effective IDs are compared, so an approved merge removes the conflict
    without rewriting stored alias ownership.
    """
    aliases = table.get("aliases", {})
    direct_ids = [str(aliases[key]) for key in _record_keys(record) if key in aliases]
    direct_unique = {effective_candidate_id(item, table) for item in direct_ids}
    explicit = str(record.get("candidate_id", "")).strip()
    explicit_effective = effective_candidate_id(explicit, table) if explicit else ""
    return len(direct_unique) > 1 or (bool(direct_unique) and explicit_effective not in {"", *direct_unique})


def resolve_candidate_id(record: dict[str, Any], table: dict[str, Any]) -> str | None:
    """Resolve a current record without inventing an author from its key.

    Exact current aliases have priority. The only fallback is the existing
    conservative identity rule against stored candidate metadata, and it is
    accepted only when one durable ID matches. Stored IDs that redirect to a
    surviving ID resolve to the survivor, so retired identities keep working
    without rewriting stored alias ownership.
    """
    return _resolve_candidate_id(record, _record_keys(record), table)


def _resolve_candidate_id(
    record: dict[str, Any], record_keys: Sequence[str], table: dict[str, Any], *, metadata_fallback: bool = True
) -> str | None:
    """Resolve *record* whose identity keys, ``_record_keys(record)``, are already derived.

    Without ``metadata_fallback`` only an alias or an explicit ID resolves it.
    """
    aliases = table.get("aliases", {})
    direct_ids = [str(aliases[key]) for key in record_keys if key in aliases]
    direct_unique = sorted({effective_candidate_id(item, table) for item in direct_ids})
    explicit = str(record.get("candidate_id", "")).strip()
    explicit_effective = effective_candidate_id(explicit, table) if explicit else ""
    if len(direct_unique) > 1:
        return None
    if direct_unique:
        return direct_unique[0] if not explicit_effective or explicit_effective == direct_unique[0] else None
    if explicit_effective:
        return explicit_effective
    if not metadata_fallback:
        return None

    matched_ids: list[str] = []
    mention = _mention(record)
    redirects = table.get("redirects", {})
    if not isinstance(redirects, dict):
        redirects = {}
    for candidate_id, metadata in sorted(table.get("candidates", {}).items()):
        if not isinstance(metadata, dict):
            continue
        if str(candidate_id) in redirects:
            continue
        stored_mention = _metadata_mention(metadata)
        if stored_mention is not None and mentions_are_identity_match(mention, stored_mention):
            matched_ids.append(effective_candidate_id(str(candidate_id), table))
    unique_ids = sorted(set(matched_ids))
    return unique_ids[0] if len(unique_ids) == 1 else None


def _preferred_record(records: list[dict[str, Any]], candidate_id: str, table: dict[str, Any]) -> dict[str, Any] | None:
    metadata = _candidate_metadata(table, candidate_id)
    preferred = str(metadata.get("preferred_history_key", "")).strip()
    if not preferred:
        return None
    matches = [record for record in records if str(record.get("identity_key", "")).strip() == preferred]
    return matches[0] if len(matches) == 1 else None


def resolve_history_record(
    record: dict[str, Any], table: dict[str, Any], history: list[dict[str, Any]] | None = None
) -> str | None:
    """Resolve the durable ID for a record, optionally using history context."""
    resolved = resolve_candidate_id(record, table)
    if resolved:
        return resolved
    if history is not None:
        _match, resolved = resolve_history_match(record, history, table)
        return resolved
    key = str(record.get("identity_key", "")).strip()
    return candidate_id_for(key) if key else None


def resolve_history_match(
    current: dict[str, Any], history: Sequence[dict[str, Any]] | KeyedHistory, table: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """Find one safe historical record and its durable ID.

    A group of legacy records with no IDs is not collapsed. When a seeded
    alias group identifies such records, its preferred history key selects the
    authoritative record for current resolution. A caller resolving many
    candidates passes one :class:`KeyedHistory` for all of them.
    """
    keyed = history if isinstance(history, KeyedHistory) else KeyedHistory.of(history)
    current_key_list = _record_keys(current)
    current_keys = set(current_key_list)
    usable = keyed.records

    def resolved_id(record: dict[str, Any]) -> str | None:
        return _resolve_candidate_id(record, keyed.keys(record), table)

    # A record stored under older admission rules is the same exact sighting
    # when today's admission of its stored title and authors gives the current
    # key (Issue #854). It is taken before the metadata fallback, which could
    # otherwise pick a compatible twin, and only when no alias, explicit ID,
    # or stored key names the sighting and a single record matches.
    if _resolve_candidate_id(current, current_key_list, table, metadata_fallback=False) is None and not any(
        str(record.get("identity_key", "")).strip() in current_keys for record in usable
    ):
        readmitted = [record for record in usable if keyed.admitted_key(record) in current_keys]
        if len(readmitted) == 1:
            record = readmitted[0]
            candidate_id = resolved_id(record) or candidate_id_for(str(record["identity_key"]))
            # A durable ID's preferred history record stays authoritative.
            same_id = [item for item in usable if resolved_id(item) == candidate_id]
            return _preferred_record(same_id, candidate_id, table) or record, candidate_id
        # Several records admit to the current key: the compatible-record
        # rules below pick a preferred record or report the ambiguity.

    current_id = resolve_candidate_id(current, table)

    if current_id:
        same_id = [record for record in usable if resolved_id(record) == current_id]
        preferred = _preferred_record(same_id, current_id, table)
        if preferred is not None:
            return preferred, current_id
        exact = [record for record in same_id if str(record.get("identity_key", "")).strip() in current_keys]
        if len(exact) == 1:
            return exact[0], current_id
        if len(same_id) == 1:
            return same_id[0], current_id
        if len(same_id) > 1:
            raise ValueError(
                f"ambiguous candidate history for durable candidate ID {current_id}: "
                "no preferred or exact history key selected one record"
            )
        # A stale alias may have no corresponding history record. This is
        # safe to treat as a new sighting and does not collapse history.
        return None, current_id

    exact = [record for record in usable if str(record.get("identity_key", "")).strip() in current_keys]
    if len(exact) == 1:
        record = exact[0]
        return record, resolved_id(record) or candidate_id_for(str(record["identity_key"]))
    if len(exact) > 1:
        raise ValueError("ambiguous candidate history: exact identity key matched multiple records")

    current_mention = _mention(current)
    compatible = [record for record in usable if mentions_are_identity_match(current_mention, _mention(record))]
    if not compatible:
        # Only when stored authors match nothing, a record whose stored authors
        # admit differently today is compared as admitted (Issue #854), so a
        # match the stored authors already decide never becomes ambiguous.
        compatible = [
            record
            for record in usable
            if (admitted := keyed.admitted_mention(record)) is not None
            and mentions_are_identity_match(current_mention, admitted)
        ]
    if not compatible:
        return None, None
    durable_ids = {
        resolved_id(record) or candidate_id_for(str(record.get("identity_key", ""))) for record in compatible
    }
    if len(compatible) == 1:
        candidate_id = next(iter(durable_ids))
        return compatible[0], candidate_id

    preferred_matches: list[tuple[dict[str, Any], str]] = []
    for candidate_id in sorted(durable_ids):
        records_for_id = [
            record
            for record in compatible
            if (resolved_id(record) or candidate_id_for(str(record.get("identity_key", "")))) == candidate_id
        ]
        preferred = _preferred_record(records_for_id, candidate_id, table)
        if preferred is not None:
            preferred_matches.append((preferred, candidate_id))
    if len(preferred_matches) == 1:
        return preferred_matches[0]
    raise ValueError("ambiguous candidate history: multiple compatible records lacked one preferred history key")


def seed_alias_table_from_migration(
    report: dict[str, Any], existing_table: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Seed all migration member keys without overwriting any alias."""
    table = validate_alias_table(existing_table)
    additions: list[dict[str, str]] = []
    reused: list[dict[str, str]] = []
    conflicts = [copy.deepcopy(item) for item in report.get("alias_conflicts", [])]
    details = report.get("identity_groups")
    if not isinstance(details, list):
        details = report.get("merge_groups", []) + [
            detail for detail in report.get("unrekeyable_records", []) if isinstance(detail, dict)
        ]
    # Unrekeyable report rows lack member details. The projected record is the
    # complete member for those rows.
    projected_by_key = {
        str(record.get("identity_key", "")): record
        for record in report.get("projected_history", [])
        if isinstance(record, dict)
    }
    for detail in details:
        if not isinstance(detail, dict):
            continue
        members = detail.get("members", [])
        if not isinstance(members, list) or not members:
            projected = projected_by_key.get(str(detail.get("stored_key", "")))
            if projected is None:
                projected = next(
                    (
                        item
                        for item in report.get("projected_history", [])
                        if str(item.get("identity_key", "")) == str(detail.get("canonical_identity_key", ""))
                    ),
                    None,
                )
            members = [
                {
                    "stored_key": str(detail.get("stored_key", "")),
                    "recomputed_key": str(detail.get("recomputed_key", "")),
                    "title": projected.get("title", "") if isinstance(projected, dict) else detail.get("title", ""),
                    "raw_authors": projected.get("primary_authors", []) if isinstance(projected, dict) else [],
                }
            ]
        projected = next(
            (
                item
                for item in report.get("projected_history", [])
                if str(item.get("identity_key", "")) == str(detail.get("canonical_identity_key", ""))
            ),
            None,
        )
        aliases = {str(member.get("stored_key", "")) for member in members if str(member.get("stored_key", "")).strip()}
        for member in members:
            raw_authors = member.get("raw_authors", [])
            if (
                str(member.get("title", "")).strip()
                and isinstance(raw_authors, list)
                and any(str(author).strip() for author in raw_authors)
            ):
                recomputed = str(member.get("recomputed_key", "")).strip()
                if recomputed:
                    aliases.add(recomputed)
        canonical_key = str(detail.get("canonical_identity_key", "")).strip()
        if canonical_key:
            aliases.add(canonical_key)
        if isinstance(projected, dict):
            projected_key = str(projected.get("identity_key", "")).strip()
            if projected_key:
                aliases.add(projected_key)
        existing_ids = {
            str(member.get("candidate_id", "")).strip()
            for member in members
            if str(member.get("candidate_id", "")).strip()
        }
        existing_ids.update(
            effective_candidate_id(str(table["aliases"][alias]), table)
            for alias in sorted(aliases)
            if alias in table["aliases"]
        )
        if isinstance(projected, dict) and str(projected.get("candidate_id", "")).strip():
            existing_ids.add(str(projected["candidate_id"]))
        if len(existing_ids) > 1:
            conflict = {
                "canonical_identity_key": canonical_key,
                "candidate_ids": sorted(existing_ids),
                "aliases": sorted(aliases),
                "reason": "multiple_durable_ids",
            }
            conflicts.append(conflict)
            continue
        candidate_id = next(iter(existing_ids), candidate_id_for(canonical_key))
        trial = copy.deepcopy(table)
        group_additions: list[dict[str, str]] = []
        group_reused: list[dict[str, str]] = []
        for alias in sorted(aliases):
            if alias in trial["aliases"]:
                if effective_candidate_id(str(trial["aliases"][alias]), trial) != effective_candidate_id(
                    candidate_id, trial
                ):
                    conflicts.append(
                        {
                            "canonical_identity_key": canonical_key,
                            "candidate_ids": sorted({candidate_id, str(trial["aliases"][alias])}),
                            "aliases": [alias],
                            "reason": "alias_already_points_to_different_candidate",
                        }
                    )
                    break
                group_reused.append({"alias": alias, "candidate_id": candidate_id})
            else:
                trial["aliases"][alias] = candidate_id
                group_additions.append({"alias": alias, "candidate_id": candidate_id})
        else:
            metadata = trial["candidates"].setdefault(candidate_id, {"candidate_id": candidate_id})
            metadata.setdefault("candidate_id", candidate_id)
            metadata.setdefault("canonical_identity_key", canonical_key)
            preferred = str(detail.get("canonical_stored_key", "")).strip()
            if not preferred:
                preferred = min(
                    (
                        str(member.get("stored_key", "")).strip()
                        for member in members
                        if str(member.get("stored_key", "")).strip()
                    ),
                    default=canonical_key,
                )
            metadata.setdefault("preferred_history_key", preferred)
            if isinstance(projected, dict):
                metadata.setdefault("title", projected.get("title", ""))
                metadata.setdefault("primary_authors", copy.deepcopy(projected.get("primary_authors", [])))
                projected_isbn = CandidateIsbn.from_record(projected)
                if projected_isbn:
                    if "isbn" not in metadata:
                        # setdefault semantics: only a missing isbn key is
                        # seeded, so a present empty key still blocks the
                        # write. The discovery-only marker travels with a
                        # learned value as one atomic pair so identity
                        # matching never treats it as strong proof across
                        # the durable alias boundary.
                        projected_isbn.apply_to(metadata)
                    else:
                        stored_isbn = CandidateIsbn.from_record(metadata)
                        if (
                            projected_isbn.discovery_only
                            and stored_isbn
                            and stored_isbn.normalized == projected_isbn.normalized
                        ):
                            # The same learned ISBN is already stored
                            # without its safety marker; restore the marker
                            # so it stays discovery-only.
                            metadata["isbn_discovery_only"] = True
                        # A different stored ISBN is trusted: it is neither
                        # overwritten nor marked, and no unrelated marker
                        # state is cleared.
            table = trial
            additions.extend(group_additions)
            reused.extend(group_reused)
            if isinstance(projected, dict):
                projected["candidate_id"] = candidate_id
    report["alias_conflicts"] = sorted(conflicts, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
    report["alias_additions"] = sorted(additions, key=lambda item: (item["alias"], item["candidate_id"]))
    report["alias_reused"] = sorted(reused, key=lambda item: (item["alias"], item["candidate_id"]))
    report["alias_additions_count"] = len(report["alias_additions"])
    report["alias_reused_count"] = len(report["alias_reused"])
    report["alias_table"] = copy.deepcopy(table)
    report["alias_conflict_count"] = len(report["alias_conflicts"])
    report["apply_blocked"] = bool(report.get("apply_blocked")) or bool(report["alias_conflicts"])
    return table
