"""Mark the review cards that a Probation Source reported.

A source on probation is still being judged, and the editor judges it by the
books it brings. This module tells the review which cards came from such a
source and how far into its trial the source is, so the review can name the
source in its own colour. It reads the source configuration only: nothing is written, no card is
hidden, and no source changes its lifecycle status. Ending a trial stays an
explicit ``source-lifecycle`` decision.

The trial dates are the fields the configuration already has:
``probation_started_at`` and ``probation_evidence_window_weeks``. A probation
source without a start date is marked without a week count, and one without a
window never reads as overdue.
"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.config import resolve_weekly_portfolio_or_raise
from autoscout.json_io import read_json_or_default

PROBATION_STATUS = "probation"
TRIAL_MARK = "новый источник"
TRIAL_OVERDUE_TEXT = "срок проверки истёк, нужно решение"

# Row field the review renderers read.
TRIAL_SOURCES_FIELD = "trial_sources"


@dataclass(frozen=True)
class TrialSource:
    """One active source on probation, with its trial dates when recorded."""

    name: str
    started_at: str = ""
    window_weeks: int = 0

    def as_row_entry(self) -> dict[str, Any]:
        return {"name": self.name, "started_at": self.started_at, "window_weeks": self.window_weeks}


def trial_sources(sources: Iterable[Any]) -> dict[str, TrialSource]:
    """The active sources on probation, by name."""
    trials: dict[str, TrialSource] = {}
    for source in sources:
        if not isinstance(source, Mapping) or source.get("active", True) is False:
            continue
        if str(source.get("lifecycle_status", "")).strip() != PROBATION_STATUS:
            continue
        name = str(source.get("name", "")).strip()
        if not name:
            continue
        window = source.get("probation_evidence_window_weeks")
        trials[name] = TrialSource(
            name=name,
            started_at=str(source.get("probation_started_at", "") or "").strip(),
            window_weeks=window if isinstance(window, int) and not isinstance(window, bool) and window > 0 else 0,
        )
    return trials


def trial_sources_from_config(config_path: Path) -> dict[str, TrialSource]:
    """Read the sources on probation from a weekly configuration file.

    A missing or unreadable file is an error, as for the withdrawn sources: a
    render never mistakes a wrong path for "no source is on probation".
    """
    if not config_path.is_file():
        raise ValueError(f"configuration file not found: {config_path}")
    raw = read_json_or_default(config_path, {})
    if not isinstance(raw, dict):
        raise ValueError(f"configuration is not a JSON object: {config_path}")
    sources = resolve_weekly_portfolio_or_raise(raw, config_path).get("sources", [])
    return trial_sources(sources if isinstance(sources, list) else [])


def row_trial_fields(source_names: Iterable[str], trials: Mapping[str, TrialSource]) -> dict[str, Any]:
    """The trial field of one review row: the probation sources among the sources reporting its book."""
    marked = sorted({str(name).strip() for name in source_names} & set(trials))
    return {TRIAL_SOURCES_FIELD: [trials[name].as_row_entry() for name in marked]}


def _iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def trial_week(started_at: str, run_date: str) -> int:
    """The 1-based week of the trial on *run_date*, 0 when either date is unknown."""
    started, today = _iso_date(started_at), _iso_date(run_date)
    if started is None or today is None:
        return 0
    return max((today - started).days, 0) // 7 + 1


def trial_mark_text(entry: Mapping[str, Any], run_date: str) -> str:
    """«новый источник: Verso — Forthcoming · неделя 2 из 3» for one trial source of a row.

    Without a start date the week is left out; without a window the week
    stands alone; past the window the mark says a decision is due.
    """
    name = str(entry.get("name", "")).strip()
    text = f"{TRIAL_MARK}: {name}" if name else TRIAL_MARK
    week = trial_week(str(entry.get("started_at", "")), run_date)
    if not week:
        return text
    window = entry.get("window_weeks")
    window = window if isinstance(window, int) and window > 0 else 0
    if not window:
        return f"{text} · неделя {week}"
    if week > window:
        return f"{text} · {TRIAL_OVERDUE_TEXT}"
    return f"{text} · неделя {week} из {window}"
