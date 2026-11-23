"""The marker a Candidate History record keeps when an archive backfill added it.

A backfill adds a whole archive at once: the 2026 Publishers Weekly archive
put 728 books into Candidate History in one day. Each keeps the date of its
PW issue as its first-seen date, and shares its source name with the weekly
PW pass, so nothing on the record said it came from the batch and the review
could not show the batch in one place.

The marker names the batch and the day it was applied. It is written once,
when the record enters history, and nothing clears it: a weekly run, a
description stage and an editor decision all leave it alone, and a merge
keeps it. The review reads it to show the batch books that still wait for a
decision as their own block.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from typing import Any

ARCHIVE_BACKFILL_FIELD = "archive_backfill"

_BATCH_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_PW_ARCHIVE_BATCH = re.compile(r"pw-archive-(\d{4})")


def pw_archive_batch(year: Any) -> str:
    """The batch name of one year of the Publishers Weekly PDF archive."""
    return f"pw-archive-{int(year):04d}"


def archive_backfill_marker(batch: str, applied_date: str) -> dict[str, str]:
    """The marker for *batch*, applied on *applied_date* (ISO). Raises ``ValueError`` on a bad value."""
    batch = str(batch or "").strip()
    if not _BATCH_ID.fullmatch(batch):
        raise ValueError("archive_backfill_batch_invalid")
    try:
        applied = date.fromisoformat(str(applied_date or "").strip())
    except ValueError as exc:
        raise ValueError("archive_backfill_applied_date_invalid") from exc
    return {"batch": batch, "applied_date": applied.isoformat()}


def archive_backfill(record: Any) -> dict[str, str] | None:
    """The marker on *record*, or ``None`` when it carries none or a malformed one."""
    marker = record.get(ARCHIVE_BACKFILL_FIELD) if isinstance(record, Mapping) else None
    if not isinstance(marker, Mapping):
        return None
    try:
        return archive_backfill_marker(str(marker.get("batch", "")), str(marker.get("applied_date", "")))
    except ValueError:
        return None


def earliest_archive_backfill(records: Any) -> dict[str, str] | None:
    """The earliest-applied marker among *records*: the one a merged record keeps."""
    markers = [marker for record in records if (marker := archive_backfill(record)) is not None]
    if not markers:
        return None
    return min(markers, key=lambda marker: (marker["applied_date"], marker["batch"]))


def archive_batch_label(batch: str) -> str:
    """The editor-facing Russian name of a batch."""
    match = _PW_ARCHIVE_BATCH.fullmatch(str(batch or "").strip())
    if match:
        return f"Архив PW {match.group(1)}"
    return f"Архив: {batch}"
