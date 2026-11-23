"""The last collected list of each annual and monthly source (#1057).

An annual or monthly source is collected only on the collection weekday. On
the other nights the pipeline carries its books forward from the list it
reported when it was last collected. That list is kept here, in the state
directory as ``scheduled-source-lists.json``, keyed by source name, so it does
not depend on which output directory the previous run wrote to, and a damaged
or regenerated output artifact on a weekday cannot lose it.

Only a fresh collection that reported mentions replaces a source's recorded
list; a reused artifact from an earlier operation does not. A failed or empty collection keeps the good list; a shorter
successful list replaces it, so a real drop stays a real drop.
"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from autoscout.collection_schedule import CARRIED_FROM_FIELD
from autoscout.collection_schedule import CARRIED_FROM_PREVIOUS_OUTPUT
from autoscout.collection_schedule import CARRIED_LIST_DATE_FIELD
from autoscout.collection_schedule import SCHEDULED_FREQUENCIES
from autoscout.collection_schedule import is_not_due_entry
from autoscout.collection_schedule import source_check_frequency
from autoscout.json_io import read_json_or_default
from autoscout.json_io import write_json
from autoscout.source_collection import SourceCollection
from autoscout.source_collection import SourceCollectionResult
from autoscout.source_run_status import COLLECTING_SOURCE_STATUSES
from autoscout.source_run_status import is_stale_source_run
from autoscout.source_run_status import normalized_source_run_status
from autoscout.state import read_ranking_freshness
from autoscout.state import store_ranking_freshness
from autoscout.state_store import StateStore

STATE_SCHEDULED_SOURCE_LISTS = "scheduled-source-lists.json"

# One recorded list: {"collected_on": "YYYY-MM-DD", "mentions": [...]}.
ScheduledSourceLists = dict[str, dict[str, Any]]


def load_scheduled_source_lists(state_dir: Path | None) -> ScheduledSourceLists:
    """Read the recorded lists; a missing or malformed file records nothing."""
    if state_dir is None:
        return {}
    raw = read_json_or_default(state_dir / STATE_SCHEDULED_SOURCE_LISTS, {})
    sources = raw.get("sources") if isinstance(raw, dict) else None
    if not isinstance(sources, dict):
        return {}
    lists: ScheduledSourceLists = {}
    for name, entry in sources.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        mentions = entry.get("mentions")
        if isinstance(mentions, list) and mentions and all(isinstance(mention, dict) for mention in mentions):
            lists[name] = {"collected_on": str(entry.get("collected_on", "")), "mentions": mentions}
    return lists


def updated_scheduled_source_lists(
    lists: ScheduledSourceLists,
    sources: Iterable[object],
    result: SourceCollectionResult,
    run_date: str,
) -> ScheduledSourceLists:
    """Record each annual and monthly source this run collected with mentions.

    A not-due source carried from the previous output, because no list was
    recorded for it yet, is recorded too, under the date its list was
    collected on, so that fallback is used once.
    """
    updated = {name: dict(entry) for name, entry in lists.items()}
    runs = {str(run.get("source_name", "")).strip(): run for run in result.source_runs if isinstance(run, Mapping)}
    for source in sources:
        if not isinstance(source, Mapping) or source_check_frequency(source) not in SCHEDULED_FREQUENCIES:
            continue
        name = str(source.get("name", "")).strip()
        run = runs.get(name, {})
        mentions = [mention for mention in result.source_mentions if mention.get("source_name") == name]
        if not name or not mentions:
            continue
        if is_not_due_entry(source):
            if run.get(CARRIED_FROM_FIELD) == CARRIED_FROM_PREVIOUS_OUTPUT and name not in updated:
                list_date = str(run.get(CARRIED_LIST_DATE_FIELD, "")) or run_date
                updated[name] = {"collected_on": list_date, "mentions": _redated(mentions, list_date)}
            continue
        # Only a collection made in this operation replaces the list: a source
        # that reported mentions (a collecting status) whose evidence is not
        # stale. A ``reused`` row serves an earlier operation's artifact and
        # leaves the list and its date as they are.
        status = normalized_source_run_status(run.get("status"))
        if status in COLLECTING_SOURCE_STATUSES and not is_stale_source_run(status):
            updated[name] = {"collected_on": run_date, "mentions": mentions}
    return updated


def _redated(mentions: list[dict[str, Any]], list_date: str) -> list[dict[str, Any]]:
    """Give carried mentions back the date their list was collected on."""
    return [
        {**mention, "source_check_date": list_date} if "source_check_date" in mention else mention
        for mention in mentions
    ]


def write_scheduled_source_lists(state_dir: Path | None, lists: ScheduledSourceLists) -> None:
    """Retain the recorded lists, when state exists and something is recorded."""
    if state_dir is None or (not lists and not (state_dir / STATE_SCHEDULED_SOURCE_LISTS).is_file()):
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        state_dir / STATE_SCHEDULED_SOURCE_LISTS,
        {"version": 1, "sources": {name: lists[name] for name in sorted(lists)}},
    )


def collect_sources(
    config_path: Path,
    output_dir: Path,
    state_dir: Path | None,
    run_date: str,
    sources: list[dict[str, Any]],
    *,
    state_store: StateStore | None,
) -> SourceCollectionResult:
    """Collect the run's sources, carrying the not-due ones, and retain the state both need.

    The ranking-freshness ledger is canonical state and goes through the run's
    *state_store*; the scheduled lists are a file of *state_dir*.
    """
    lists = load_scheduled_source_lists(state_dir)
    result = SourceCollection(
        config_path, output_dir, run_date, read_ranking_freshness(state_store)
    ).collect_on_schedule(sources, lists)
    store_ranking_freshness(state_store, result.ranking_freshness)
    write_scheduled_source_lists(state_dir, updated_scheduled_source_lists(lists, sources, result, run_date))
    return result
