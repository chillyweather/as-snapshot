"""When a source is due for collection (#1057).

The weekly run goes nightly. A prize list (``check_frequency: annual``) or a
monthly list changes a few times a year, so capturing and extracting it every
night costs a fetch and a model call for nothing. Such a source is collected on
one fixed weekday, the collection weekday, and is *not due* on the other days.
The weekday is Sunday unless the weekly config sets ``collection_weekday``. The
rule is by run date, so a manual run and a scheduled run on the same date agree.
Weekly and daily sources, and any frequency this module does not know, are due
every day.

A not-due source is neither captured nor extracted. Its source run carries the
status ``not-due``: not a failure, not a skip, not stale evidence.

Its books stay as they are. The pipeline carries the source's last collected
list forward (``autoscout.scheduled_source_lists``), re-dated to the run date, so grouping, signals, priority,
the update delta and Candidate History see the same list they saw when the
source was last collected. A book that two sources report keeps both signals,
and the next collection of an unchanged list is not news. The run date is put
on the carried mentions because a weekly report holds one date; the list itself
is unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Mapping
from datetime import date
from datetime import timedelta
from typing import Any

from autoscout.source_metadata import portable_source_metadata

COLLECTION_WEEKDAY_FIELD = "collection_weekday"
WEEKDAYS: tuple[str, ...] = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
DEFAULT_COLLECTION_WEEKDAY = "sunday"
# Frequencies collected only on the collection weekday.
SCHEDULED_FREQUENCIES = frozenset({"annual", "monthly"})

# Fields on a replay-config source entry and on its source run.
NOT_DUE_FIELD = "collection_not_due"
NEXT_DUE_DATE_FIELD = "next_due_date"
CARRIED_MENTION_COUNT_FIELD = "carried_mention_count"
# Where a not-due source's carried list came from, and when it was collected.
CARRIED_FROM_FIELD = "carried_from"
CARRIED_LIST_DATE_FIELD = "carried_list_date"
CARRIED_FROM_RECORDED_LIST = "recorded-list"
CARRIED_FROM_PREVIOUS_OUTPUT = "previous-output"
CARRIED_FROM_NOTHING = "nothing"
# On a worklist entry only: how many work items precede the source.
WORK_ITEM_POSITION_FIELD = "work_item_position"

_HELPER_FIELDS = frozenset({"helper_metadata_file", "helper_metadata_sha256"})

# The dates a source mention carries; a carried mention takes the run date.
_MENTION_DATE_FIELDS = ("source_check_date", "run_date")


def collection_weekday_error(value: object) -> str:
    """Return why *value* is not a collection weekday, or "" when it is one."""
    if not isinstance(value, str) or value.strip().casefold() not in WEEKDAYS:
        return f"'{COLLECTION_WEEKDAY_FIELD}' must be one of: {', '.join(WEEKDAYS)}"
    return ""


def collection_weekday(config: Mapping[str, Any]) -> str:
    """Return the configured collection weekday, Sunday by default.

    Raises ``ValueError`` for a value that names no weekday, so a typo never
    turns into "collect every annual source every day" or "never".
    """
    value = config.get(COLLECTION_WEEKDAY_FIELD, DEFAULT_COLLECTION_WEEKDAY)
    error = collection_weekday_error(value)
    if error:
        raise ValueError(error)
    return str(value).strip().casefold()


def source_check_frequency(source: Mapping[str, Any]) -> str:
    """Return a source's declared frequency, normalized, accepting the ``refresh_cadence`` alias."""
    return declared_check_frequency(source).casefold()


def declared_check_frequency(source: Mapping[str, Any]) -> str:
    """The frequency a replay entry carries: ``check_frequency``, else its ``refresh_cadence`` alias.

    The value is kept as declared, so it still equals the alias the entry may
    also carry, which the config validator requires. A replay entry that
    defaulted to weekly would be read as weekly by every later reader while
    the capture, which reads the alias, treated the source as annual.
    """
    for field in ("check_frequency", "refresh_cadence"):
        value = str(source.get(field, "") or "").strip()
        if value:
            return value
    return "weekly"


def is_source_due(check_frequency: str, run_date: date, weekday: str = DEFAULT_COLLECTION_WEEKDAY) -> bool:
    """Return whether a source of *check_frequency* is collected on *run_date*."""
    if str(check_frequency).strip().casefold() not in SCHEDULED_FREQUENCIES:
        return True
    return run_date.weekday() == WEEKDAYS.index(weekday)


def next_due_date(run_date: date, weekday: str = DEFAULT_COLLECTION_WEEKDAY) -> date:
    """Return the first collection weekday after *run_date*."""
    days_ahead = (WEEKDAYS.index(weekday) - run_date.weekday()) % 7
    return run_date + timedelta(days=days_ahead or 7)


def not_due_reason(check_frequency: str, weekday: str, next_due: date) -> str:
    """The recorded reason of a not-due source run, in operator terms."""
    return f"not due: a {check_frequency} source is collected on {weekday.capitalize()}; next on {next_due.isoformat()}"


def not_due_source_entry(source: Mapping[str, Any], run_date: date, weekday: str) -> dict[str, Any]:
    """The replay-config entry that tells the pipeline to carry a source forward."""
    frequency = source_check_frequency(source)
    next_due = next_due_date(run_date, weekday)
    # The declared metadata a collected source carries into replay, so the
    # source's metrics and lifecycle read the same on every night. The helper
    # sidecar is left out: only a collector reads it, to extract a captured
    # page, and nothing is extracted for a not-due source. Its file and digest
    # are declared together or not at all.
    metadata = {
        key: value for key, value in portable_source_metadata(dict(source)).items() if key not in _HELPER_FIELDS
    }
    return {
        **metadata,
        "name": str(source.get("name", "")),
        "family": str(source.get("family", "")),
        "url": str(source.get("url", "")),
        "access_status": str(source.get("access_status", "open")),
        # As declared, so it equals a refresh_cadence alias the entry carries.
        "check_frequency": declared_check_frequency(source),
        NOT_DUE_FIELD: True,
        NEXT_DUE_DATE_FIELD: next_due.isoformat(),
        "notes": not_due_reason(frequency, weekday, next_due),
    }


def with_not_due_entries(sources: list[dict[str, Any]], entries: object) -> list[dict[str, Any]]:
    """Place each not-due entry among the replay sources where its work item would be.

    Finalization lists the work items first, in configuration order. Putting a
    not-due source back in its own place keeps the order the pipeline sees on
    a night that collected it, so a tie between two sources' mentions of one
    book resolves the same way on every night.
    """
    placed: list[tuple[int, dict[str, Any]]] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, Mapping):
            continue
        replay_entry = {key: value for key, value in entry.items() if key != WORK_ITEM_POSITION_FIELD}
        position = entry.get(WORK_ITEM_POSITION_FIELD)
        if isinstance(position, bool) or not isinstance(position, int) or not 0 <= position <= len(sources):
            position = len(sources)
        placed.append((position, replay_entry))
    merged: list[dict[str, Any]] = []
    for index in range(len(sources) + 1):
        merged.extend(entry for position, entry in placed if position == index)
        if index < len(sources):
            merged.append(sources[index])
    return merged


def is_not_due_entry(source: Mapping[str, Any]) -> bool:
    """Return whether a replay-config source entry names a not-due source."""
    return source.get(NOT_DUE_FIELD) is True


def carried_forward_mentions(previous_mentions: Iterable[object], source_name: str, run_date: str) -> list[Any]:
    """Return the mentions of *source_name* in a last collected list, re-dated to *run_date*.

    A source with no recorded list and no mention in the previous output (a
    newly onboarded source) carries nothing.
    """
    name = source_name.strip()
    carried: list[Any] = []
    for mention in previous_mentions:
        if not isinstance(mention, Mapping) or str(mention.get("source_name", "")).strip() != name or not name:
            continue
        redated = dict(mention)
        for field in _MENTION_DATE_FIELDS:
            if field in redated:
                redated[field] = run_date
        carried.append(redated)
    return carried
