"""Canonical state for the MVP Pipeline, through the State Store (#1068).

These functions are thin wrappers over ``autoscout.state_store`` kept for the
transition. The weekly run passes its own store to the store-taking forms
(``read_run_state``, ``save_run_state``, ``read_ranking_freshness``,
``store_ranking_freshness``); the ``state_dir`` forms open one with the
backend the environment selects. No file name is known here: the layout
belongs to the State Store (#1070).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from autoscout.candidate_identity import empty_alias_table
from autoscout.ranking_freshness import RankingFreshnessLedger
from autoscout.state_store import Collection
from autoscout.state_store import StateStore
from autoscout.state_store import open_state_store


def load_ranking_freshness(state_dir: Path | None) -> RankingFreshnessLedger | None:
    """Return the retained undated-ranking ledger, or ``None`` without state.

    ``None`` is not an empty ledger.  An undated ranking cannot prove movement
    without retained fingerprints, so it fails closed rather than collecting.
    """
    return read_ranking_freshness(None if state_dir is None else open_state_store(state_dir))


def read_ranking_freshness(store: StateStore | None) -> RankingFreshnessLedger | None:
    """Return the retained undated-ranking ledger from *store*, or ``None`` without state."""
    if store is None:
        return None
    return RankingFreshnessLedger.from_records(store.read(Collection.RANKING_FRESHNESS))


def write_ranking_freshness(state_dir: Path | None, ledger: RankingFreshnessLedger | None) -> None:
    """Retain the observed undated-ranking fingerprints, when state exists."""
    if state_dir is None:
        return
    store_ranking_freshness(open_state_store(state_dir), ledger)


def store_ranking_freshness(store: StateStore | None, ledger: RankingFreshnessLedger | None) -> None:
    """Retain the observed undated-ranking fingerprints in *store*, when state exists."""
    if store is None or ledger is None:
        return
    store.replace(Collection.RANKING_FRESHNESS, ledger.to_records())


def load_state(state_dir: Path | None) -> dict[str, Any]:
    return read_run_state(None if state_dir is None else open_state_store(state_dir))


def read_run_state(store: StateStore | None) -> dict[str, Any]:
    """The canonical state a weekly run starts from; empty values without state."""
    if store is None:
        return {
            "candidate_history": [],
            "excluded_candidate_history": [],
            "pending_feedback": [],
            "candidate_visibility": [],
            "source_proposals": [],
            "editorial_taste_profile": {"entries": []},
            "candidate_identity_aliases": empty_alias_table(),
        }
    return {
        "candidate_history": store.read(Collection.CANDIDATE_HISTORY),
        "excluded_candidate_history": store.read(Collection.EXCLUDED_CANDIDATE_HISTORY),
        "pending_feedback": store.read(Collection.PENDING_EDITOR_ROWS),
        "candidate_visibility": store.read(Collection.CANDIDATE_VISIBILITY),
        "source_proposals": store.read(Collection.SOURCE_PROPOSALS),
        "editorial_taste_profile": store.read(Collection.EDITORIAL_PROFILE),
        "candidate_identity_aliases": store.read(Collection.CANDIDATE_IDENTITY_ALIASES),
    }


def save_state(
    state_dir: Path | None,
    candidate_history: list[dict[str, Any]],
    excluded_candidate_history: list[dict[str, Any]],
    scouting_candidates: list[dict[str, Any]],
    consumed_feedback: list[dict[str, Any]],
    source_proposals: list[dict[str, Any]] | None = None,
    candidate_visibility: list[dict[str, Any]] | None = None,
    candidate_identity_aliases: dict[str, Any] | None = None,
) -> None:
    """Save a weekly run's state in one State Store transaction (see ``save_run_state``)."""
    if state_dir is None:
        return
    save_run_state(
        open_state_store(state_dir),
        candidate_history,
        excluded_candidate_history,
        scouting_candidates,
        consumed_feedback,
        source_proposals,
        candidate_visibility=candidate_visibility,
        candidate_identity_aliases=candidate_identity_aliases,
    )


def save_run_state(
    store: StateStore | None,
    candidate_history: list[dict[str, Any]],
    excluded_candidate_history: list[dict[str, Any]],
    scouting_candidates: list[dict[str, Any]],
    consumed_feedback: list[dict[str, Any]],
    source_proposals: list[dict[str, Any]] | None = None,
    candidate_visibility: list[dict[str, Any]] | None = None,
    candidate_identity_aliases: dict[str, Any] | None = None,
) -> None:
    """Save a weekly run's state in one State Store transaction.

    Consumed editor rows are appended to the archive and the pending rows are
    emptied. Visibility and the alias table are kept as stored when not given.
    """
    if store is None:
        return
    with store.transaction():
        store.replace(Collection.CANDIDATE_HISTORY, candidate_history)
        store.replace(Collection.EXCLUDED_CANDIDATE_HISTORY, excluded_candidate_history)
        store.replace(Collection.LATEST_SCOUTING_CANDIDATES, scouting_candidates)
        if candidate_visibility is None:
            candidate_visibility = store.read(Collection.CANDIDATE_VISIBILITY)
        if not isinstance(candidate_visibility, list):
            candidate_visibility = []
        store.replace(Collection.CANDIDATE_VISIBILITY, candidate_visibility)
        store.replace(Collection.SOURCE_PROPOSALS, source_proposals or [])
        if consumed_feedback:
            archive = store.read(Collection.ARCHIVED_EDITOR_ROWS)
            archive.extend(consumed_feedback)
            store.replace(Collection.ARCHIVED_EDITOR_ROWS, archive)
        store.replace(Collection.PENDING_EDITOR_ROWS, [])
        if candidate_identity_aliases is not None:
            store.replace(Collection.CANDIDATE_IDENTITY_ALIASES, candidate_identity_aliases)
