"""The weekly-config keys of the Russian-edition stage of the weekly run (#1181).

All keys are optional. :data:`SWITCH` turns the stage on; absent or false,
the weekly run never starts it. The other keys override the pass's limits
and name sources not to ask. The module stays light (it imports only the
source names) so the config validator can use it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoscout.json_io import read_json
from autoscout.russian_edition_sources import CATALOGUE_SOURCES
from autoscout.russian_edition_sources import SOURCES

SWITCH = "russian_edition_completion"
MAX_BOOKS_KEY = "russian_edition_max_books"
MAX_LIVELIB_SEARCHES_KEY = "russian_edition_max_livelib_searches"
BUDGET_SECONDS_KEY = "russian_edition_budget_seconds"
SKIP_SOURCES_KEY = "russian_edition_skip_sources"
CONFIG_KEYS = (SWITCH, MAX_BOOKS_KEY, MAX_LIVELIB_SEARCHES_KEY, BUDGET_SECONDS_KEY, SKIP_SOURCES_KEY)


@dataclass(frozen=True)
class WeeklySettings:
    """The limits the weekly config sets; ``None`` keeps the pass's own default."""

    max_books: int | None = None
    max_livelib_searches: int | None = None
    budget_seconds: float | None = None
    enabled_sources: tuple[str, ...] = SOURCES


def _count_error(key: str, value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return f"'{key}' must be a whole number, 0 or more"
    return ""


def _seconds(value: object) -> float | None:
    """``value`` as a finite number of seconds above 0, or ``None`` when it is not one.

    Never raises: an integer too large for a float (JSON allows one) is not usable.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        seconds = float(value)
    except OverflowError:
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def config_errors(config: Mapping[str, Any]) -> list[str]:
    """Why the Russian-edition keys of ``config`` are not usable; empty when they are or are absent."""
    errors: list[str] = []
    if SWITCH in config and not isinstance(config[SWITCH], bool):
        errors.append(f"'{SWITCH}' must be true or false")
    for key in (MAX_BOOKS_KEY, MAX_LIVELIB_SEARCHES_KEY):
        if key in config and (error := _count_error(key, config[key])):
            errors.append(error)
    if BUDGET_SECONDS_KEY in config and _seconds(config[BUDGET_SECONDS_KEY]) is None:
        errors.append(f"'{BUDGET_SECONDS_KEY}' must be a number of seconds above 0")
    if SKIP_SOURCES_KEY in config:
        skipped = config[SKIP_SOURCES_KEY]
        if not isinstance(skipped, list) or any(
            not isinstance(source, str) or source not in SOURCES for source in skipped
        ):
            errors.append(f"'{SKIP_SOURCES_KEY}' must be a list of source names from: {', '.join(SOURCES)}")
        elif set(CATALOGUE_SOURCES) <= set(skipped):
            # Without the State Library or LitRes nothing can be confirmed.
            errors.append(f"'{SKIP_SOURCES_KEY}' must leave {' or '.join(CATALOGUE_SOURCES)} to ask")
    return errors


def enabled(config: Any) -> bool:
    """True only when the switch is literally true."""
    return isinstance(config, Mapping) and config.get(SWITCH) is True


def config_enabled(config_path: Path | None) -> bool:
    """The switch, read from the weekly config file; a missing or unreadable config is off."""
    if config_path is None:
        return False
    try:
        config = read_json(Path(config_path))
    except (OSError, ValueError):
        return False
    return enabled(config)


def config_switch_malformed(config_path: Path | None) -> bool:
    """True when the weekly config sets the switch to something other than true or false.

    The lookups stay off for such a value, as for an absent one; the weekly
    run names it so the operator learns the switch was not read as on. A
    missing or unreadable config is not malformed here: it is off.
    """
    if config_path is None:
        return False
    try:
        config = read_json(Path(config_path))
    except (OSError, ValueError):
        return False
    return isinstance(config, Mapping) and SWITCH in config and not isinstance(config[SWITCH], bool)


def weekly_settings(config: Mapping[str, Any]) -> WeeklySettings:
    """The limits of ``config``; raise ``ValueError`` when a key is not usable."""
    errors = config_errors(config)
    if errors:
        raise ValueError("; ".join(errors))
    skipped = set(config.get(SKIP_SOURCES_KEY) or [])
    budget = config.get(BUDGET_SECONDS_KEY)
    return WeeklySettings(
        max_books=config.get(MAX_BOOKS_KEY),
        max_livelib_searches=config.get(MAX_LIVELIB_SEARCHES_KEY),
        budget_seconds=None if budget is None else _seconds(budget),
        enabled_sources=tuple(source for source in SOURCES if source not in skipped),
    )


__all__ = [
    "BUDGET_SECONDS_KEY",
    "CONFIG_KEYS",
    "MAX_BOOKS_KEY",
    "MAX_LIVELIB_SEARCHES_KEY",
    "SKIP_SOURCES_KEY",
    "SWITCH",
    "WeeklySettings",
    "config_enabled",
    "config_errors",
    "config_switch_malformed",
    "enabled",
    "weekly_settings",
]
