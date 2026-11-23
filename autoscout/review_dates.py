"""The order the editor reads the review in: by the date each book was added (#1057).

With a run every night, "new since the previous run" stops matching how the
editor reads, so the review lists its cards under dates, newest first, and an
updated book carries a mark with the date of its change instead of moving to a
group of its own. The HTML and the Markdown review both take their order from
here, so the two always agree.

``review_date`` alone decides the date a card is listed under. The operator
may still choose to list an updated book under the date of its change rather
than the date it was added; that choice is a change to that one function.

The updated mark is not tied to one run (#1063): a book carries it while its
last material change is inside a window of ``UPDATED_WINDOW_DAYS`` counted
back from the review date, and the «Обновлённые» tab lists exactly those books.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import date
from datetime import timedelta
from typing import Any

from autoscout.review_presentation import LIFECYCLE_LABELS
from autoscout.review_presentation import lifecycle_label

# Month names in the genitive, as a Russian date is written: «5 октября 2026».
RUSSIAN_MONTHS_GENITIVE = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

UNKNOWN_DATE_HEADING = "Дата неизвестна"
UNKNOWN_CHANGE_DATE_HEADING = "Дата изменения неизвестна"
UPDATED_MARK = "⬆ обновлено"
# What happens to an archive book once decided, said in the HTML block note and the Markdown archive line.
ARCHIVE_DECIDED_SENTENCE = "Книга с решением выходит из блока и стоит в списке под датой добавления пакета."
# How long a book carries the updated mark after its last material change, in
# days counted back from the review date, that date included (#1063).
UPDATED_WINDOW_DAYS = 14


def russian_days(count: int) -> str:
    """«1 день», «3 дня», «14 дней»: the count with the Russian plural form of «день»."""
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} день"
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return f"{count} дня"
    return f"{count} дней"


# The note of the «Обновлённые» tab, built from the window so the two cannot drift apart.
UPDATED_TAB_NOTE = (
    f"Книги, изменившиеся за последние {russian_days(UPDATED_WINDOW_DAYS)}, под датой изменения. "
    "Каждая из них остаётся и в общем списке под датой добавления."
)


def added_date(row: dict[str, Any]) -> str:
    """The day a book entered AutoScout, as an ISO date, or "".

    For a book from an archive backfill that is the day the batch was applied:
    its first-seen date is the date of the archive issue, which would scatter
    one batch over every issue of the year.
    """
    return str(row.get("archive_applied_date", "") or row.get("first_seen_date", "")).strip()


def review_date(row: dict[str, Any]) -> str:
    """The date a card is listed under in the review: the day it was added.

    Every rendering groups and orders by this function and by nothing else.
    """
    return added_date(row)


def _iso_day(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def is_marked_updated(row: dict[str, Any], review_day: str) -> bool:
    """True for a card that carries the updated mark on the review of *review_day*.

    A card with a last material change date is marked when
    ``review_day - (UPDATED_WINDOW_DAYS - 1) days <= change date``: with 14
    days and a review on 2026-11-14, a change on 2026-11-01 is marked and one
    on 2026-10-31 is not. A change date after the review date is inside too.
    A card with no change date is marked only when its lifecycle says it was
    updated in the run, as before the window existed. Without a readable
    review date no change date is inside the window.
    """
    changed = change_date(row)
    if not changed:
        label = lifecycle_label(row.get("lifecycle_label") or row.get("update_delta", ""))
        return label == LIFECYCLE_LABELS["upgraded"]
    day, review = _iso_day(changed), _iso_day(review_day)
    if day is None or review is None:
        return False
    return review - timedelta(days=UPDATED_WINDOW_DAYS - 1) <= day


def change_date(row: dict[str, Any]) -> str:
    """The ISO date of the card's last material change, or ""."""
    return str(row.get("last_material_change_date", "") or "").strip()


def _date_parts(iso: str) -> tuple[str, str, str] | None:
    parts = iso.split("-")
    if len(parts) != 3 or not all(part.isdigit() for part in parts) or len(parts[0]) != 4:
        return None
    if not 1 <= int(parts[1]) <= 12 or len(parts[1]) != 2 or len(parts[2]) != 2:
        return None
    return parts[0], parts[1], parts[2]


def long_russian_date(iso: str) -> str:
    """«5 октября 2026» for "2026-10-05"; any other text is returned as it is."""
    parts = _date_parts(iso)
    if parts is None:
        return iso
    year, month, day = parts
    return f"{int(day)} {RUSSIAN_MONTHS_GENITIVE[int(month) - 1]} {year}"


def short_russian_date(iso: str, run_date: str) -> str:
    """«12 ноября» for a date in the run's year, «12 ноября 2025» otherwise."""
    parts = _date_parts(iso)
    if parts is None:
        return iso
    year, month, day = parts
    text = f"{int(day)} {RUSSIAN_MONTHS_GENITIVE[int(month) - 1]}"
    return text if run_date[:4] == year else f"{text} {year}"


def updated_mark_text(row: dict[str, Any], run_date: str) -> str:
    """«⬆ обновлено 12 ноября» for a marked card, «⬆ обновлено» without a change date, "" when unmarked."""
    if not is_marked_updated(row, run_date):
        return ""
    changed = change_date(row)
    return f"{UPDATED_MARK} {short_russian_date(changed, run_date)}" if changed else UPDATED_MARK


def updated_cell_text(row: dict[str, Any], review_day: str) -> str:
    """The Markdown «Обновлено» cell: the ISO change date of a marked card, «обновлено» without one, "" when unmarked."""
    if not is_marked_updated(row, review_day):
        return ""
    return change_date(row) or "обновлено"


@dataclass
class DateSection:
    """The cards listed under one date, or one archive block.

    ``date`` is the ISO date of a dated section and "" for the cards with no
    date. An archive block has its ``archive_batch`` and the first day the
    batch was applied as its date: it is never split by date.
    """

    date: str
    archive_batch: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)


def date_sections(rows: list[dict[str, Any]]) -> list[DateSection]:
    """*rows* under their dates, newest first, page order within a date.

    The books of an archive backfill that still wait for a decision stay one
    block per batch, placed after the dated cards of the day the batch was
    applied. Cards with no date come last.
    """
    by_key: dict[tuple[str, str], DateSection] = {}
    for row in rows:
        batch = str(row.get("archive_batch", "")).strip() if row.get("archive_pending") else ""
        key = ("archive", batch) if batch else ("date", review_date(row))
        section = by_key.setdefault(key, DateSection(date="" if batch else key[1], archive_batch=batch))
        section.rows.append(row)
    # A batch applied over several days keeps one date, its first day, whichever
    # of its books still wait: the decided ones count towards it too.
    batch_dates: dict[str, str] = {}
    for row in rows:
        batch = str(row.get("archive_batch", "")).strip()
        if batch:
            batch_dates[batch] = min(batch_dates.get(batch, added_date(row)), added_date(row))
    for section in by_key.values():
        if section.archive_batch:
            section.date = batch_dates[section.archive_batch]
    # Stable sorts, last key first: a dated section before an archive block of
    # the same day, the newest date first, and the cards with no date last.
    sections = sorted(by_key.values(), key=lambda section: (section.archive_batch != "", section.archive_batch))
    sections.sort(key=lambda section: section.date, reverse=True)
    sections.sort(key=lambda section: section.date == "")
    return sections


def reading_order(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """*rows* in the order the review lists them."""
    return [row for section in date_sections(rows) for row in section.rows]


def added_on_run_date_count(rows: list[dict[str, Any]], run_date: str) -> int:
    """How many cards outside an archive block were added on *run_date*."""
    return sum(1 for row in rows if not row.get("archive_pending") and run_date and added_date(row) == run_date)


def marked_updated_count(rows: list[dict[str, Any]], review_day: str) -> int:
    """How many cards carry the updated mark."""
    return sum(1 for row in rows if is_marked_updated(row, review_day))


def updated_sections(rows: list[dict[str, Any]], review_day: str) -> list[DateSection]:
    """The marked cards of *rows* under the date of their change, newest change first.

    Page order inside a date; the marked cards with no change date come last,
    in a section whose ``date`` is "".
    """
    by_date: dict[str, DateSection] = {}
    for row in rows:
        if is_marked_updated(row, review_day):
            changed = change_date(row)
            by_date.setdefault(changed, DateSection(date=changed)).rows.append(row)
    sections = sorted(by_date.values(), key=lambda section: section.date, reverse=True)
    sections.sort(key=lambda section: section.date == "")
    return sections
