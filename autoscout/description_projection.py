"""Identity-aware projection of authoritative description fields (#671).

Candidate History is authoritative for Content Descriptions. Every route
that writes descriptions (grounded enrichment, adjudication, standalone
generation, legacy enrichment) must show the same description facts in
the current and latest Scouting Candidates without changing candidate
membership or unrelated fields.

The recovery boundary is deliberate and documented here because every
caller depends on it:

* Canonical persistence (Candidate History) always lands first.
* Derived publication (projected current/latest candidates and the
  reports recomputed from them) lands second.
* Projection is a pure function of Candidate History: re-running it over
  the same history yields byte-identical candidates. An interruption
  between canonical persistence and derived publication therefore
  recovers by re-projection alone — no history rewrite, no second agent
  call, no lost descriptions. Callers that publish through the guarded
  bundle keep that atomicity; callers that publish derived files with
  plain writes rely on this idempotence instead, and say so at the call
  site.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

from autoscout.candidate_identity import resolve_history_match
from autoscout.json_io import read_json_or_default
from autoscout.json_io import write_json
from autoscout.state_store import Collection
from autoscout.state_store import StateStore

# The description facts Candidate History owns. Every projected candidate
# carries exactly these fields from its matched history record — no more,
# so a projection can never leak editorial state (status, notes, taste
# profile effects) into a candidate snapshot, and no fewer, so obsolete
# description metadata cannot survive beside fresh text.
DESCRIPTION_PROJECTION_FIELDS = (
    "content_description",
    "description_provenance",
    "grounded_outcome",
    "content_description_rejection_reason",
    "grounded_description_attempts",
    "description_not_yet_published_date",
    # Issue #947: a book another source cited earlier is not PM-only, and
    # the coverage count reads that from the snapshot too.
    "cited_beyond_pm_deals",
)

# ``content_description`` is the payload; the other five fields are metadata
# about that payload. A history record that never learned a payload (a
# brand-new record, a hand-maintained file) must not destroy editor-visible
# copy the candidate snapshot already carries, so the payload is only ever
# overwritten, never cleared. Stale metadata has no such excuse: a metadata
# field the authority does not hold is removed, so a repaired description
# never renders beside the rejection reason or stage outcome of the attempt
# it replaced (issue #671).
_METADATA_PROJECTION_FIELDS = tuple(field for field in DESCRIPTION_PROJECTION_FIELDS if field != "content_description")


def _history_records_by_identity(history: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {
        str(record.get("identity_key", "")).strip(): record
        for record in history
        if isinstance(record, Mapping) and str(record.get("identity_key", "")).strip()
    }


def project_history_descriptions(
    history: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    aliases: Mapping[str, Any] | None = None,
    on_match: Callable[[dict[str, Any], Mapping[str, Any]], None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Project authoritative description fields from history onto candidates.

    Returns ``(projected_candidates, summary)`` where ``summary`` counts
    ``{"considered", "projected", "unmatched", "metadata_cleared"}``.

    * Membership, order, and every non-projection field are preserved: each
      row is deep-copied and only the projection fields are touched.
    * Matching is identity-aware: an exact identity-key hit wins, otherwise
      :func:`resolve_history_match` resolves aliases and durable IDs. A row
      with no match (or an ambiguous/erroring match) is returned untouched
      and counted as ``unmatched``.
    * A field the matched history record holds is deep-copied onto the row.
    * A metadata field the history record lacks is removed from the row and
      counted in ``metadata_cleared``.
    * A missing ``content_description`` never clears the row's text (see
      the module docstring for why).
    * ``on_match`` runs per matched ``(candidate_row, history_record)``
      pair after projection, so callers with adjacent fill-only learning
      (e.g. the grounded ISBN copy contract) keep that logic without
      forking the matcher. Hook exceptions propagate: a caller that cannot
      finish its per-match work must fail loudly rather than publish a
      half-projected set.
    """
    history_list = [dict(record) for record in history if isinstance(record, Mapping)]
    by_identity = _history_records_by_identity(history_list)
    alias_table = dict(aliases) if isinstance(aliases, Mapping) else {}
    projected_rows: list[dict[str, Any]] = []
    projected_count = 0
    unmatched_count = 0
    cleared_count = 0

    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            # Non-mapping rows are never description carriers. They pass
            # through by reference so membership is preserved exactly.
            unmatched_count += 1
            projected_rows.append(candidate)  # type: ignore[arg-type]
            continue
        row = deepcopy(dict(candidate))
        identity_key = str(candidate.get("identity_key", "")).strip()
        matched = by_identity.get(identity_key) if identity_key else None
        if matched is None:
            try:
                matched, _durable_id = resolve_history_match(dict(candidate), history_list, alias_table)
            except (TypeError, ValueError):
                matched = None
        if matched is None:
            unmatched_count += 1
            projected_rows.append(row)
            continue
        # Alias/durable-ID resolution may pair rows whose literal identity
        # keys differ. The row keeps its own identity; only description
        # facts travel across the match.
        if "content_description" in matched:
            row["content_description"] = deepcopy(matched["content_description"])
        for field_name in _METADATA_PROJECTION_FIELDS:
            if field_name in matched:
                row[field_name] = deepcopy(matched[field_name])
            elif field_name in row:
                del row[field_name]
                cleared_count += 1
        if on_match is not None:
            on_match(row, matched)
        projected_count += 1
        projected_rows.append(row)

    summary = {
        "considered": len(projected_rows),
        "projected": projected_count,
        "unmatched": unmatched_count,
        "metadata_cleared": cleared_count,
    }
    return projected_rows, summary


def project_snapshot_file(
    path: Path,
    history: Sequence[Mapping[str, Any]],
    aliases: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Project history descriptions onto one snapshot file (review finding).

    Returns the projection summary, or ``None`` when the file does not
    exist — the caller decides whether a missing snapshot is initialized,
    skipped, or an error. A file that exists but is not a list of mappings
    fails closed: silently overwriting a corrupt derived file would hide
    corruption that re-projection is meant to recover from.

    Caller-specific recovery behavior stays with the caller: the grounded
    bundle publishes snapshots atomically with history, while the
    adjudication, standalone, and legacy routes rely on idempotent
    re-projection after durable history.
    """
    if not path.exists():
        return None
    value = read_json_or_default(path, [])
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ValueError(f"{path.name} must be a list of objects")
    projected, summary = project_history_descriptions(history, value, aliases)
    write_json(path, projected)
    return summary


def load_projection_aliases(store: StateStore) -> Mapping[str, Any]:
    """The alias table projection matches with; an empty one when it cannot be read."""
    from autoscout.candidate_identity import empty_alias_table

    try:
        aliases: Mapping[str, Any] = store.read(Collection.CANDIDATE_IDENTITY_ALIASES)
    except (OSError, ValueError):
        return empty_alias_table()
    return aliases


def project_latest_snapshot(
    store: StateStore,
    history: Sequence[Mapping[str, Any]],
    aliases: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Project history descriptions onto the latest Scouting Candidates in *store*.

    Returns the projection summary, or ``None`` when the store holds no
    latest candidates: an absent snapshot is never created. A latest
    snapshot that is not a list of mappings fails closed, like
    :func:`project_snapshot_file`. The write joins the caller's store
    transaction when there is one, so history and its projection land
    together.
    """
    value = store.read(Collection.LATEST_SCOUTING_CANDIDATES)
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ValueError("the latest Scouting Candidates must be a list of objects")
    if not value:
        return None
    projected, summary = project_history_descriptions(history, value, aliases)
    store.replace(Collection.LATEST_SCOUTING_CANDIDATES, projected)
    return summary


def project_history_onto_snapshots(
    history: Sequence[Mapping[str, Any]],
    output_dir: Path,
    store: StateStore,
) -> dict[str, Any]:
    """Project history descriptions onto the current and latest snapshots.

    Runs only after Candidate History is durable. The current candidates in
    *output_dir* and the latest snapshot in *store* keep their membership
    and unrelated fields; only the shared description facts travel.
    Snapshots that do not exist are skipped, never created. A snapshot that
    exists but is not a list of mappings fails closed.

    Plain writes are deliberate: history is already durable and the
    projection is idempotent over it, so an interruption between the
    history write and these derived writes recovers by running any
    description-writing route again (see the module docstring).
    """
    aliases = load_projection_aliases(store)
    history_list = [dict(record) for record in history if isinstance(record, Mapping)]
    summary: dict[str, Any] = {"current": None, "latest": None}

    current_path = output_dir / "scouting-candidates.json"
    if current_path.exists():
        summary["current"] = project_snapshot_file(current_path, history_list, aliases)

    summary["latest"] = project_latest_snapshot(store, history_list, aliases)
    return summary
