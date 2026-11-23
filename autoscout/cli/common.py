"""Shared pieces of the command-line layer: the command record, exit codes and argument helpers."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from autoscout.git_envelope import weekly_run_in_progress
from autoscout.repository_root import repository_root

type SubParsers = argparse._SubParsersAction[argparse.ArgumentParser]

WEEKLY_RUN_IN_PROGRESS_EXIT_CODE = 6


class CliCommand(NamedTuple):
    """One subcommand: how its parser is registered and how it is handled."""

    register: Callable[[SubParsers], None]
    handle: Callable[[argparse.Namespace], int]


# The live weekly configuration of this checkout, whatever directory the
# command is run from. ``render-review`` reads its retired sources (#1025).
DEFAULT_REVIEW_CONFIG = repository_root() / "configs" / "current-weekly-pilot-live.json"


def _review_config(explicit: Path | None = None) -> Path | None:
    """The configuration a command renders the review with: the given one, else the live one when present."""
    if explicit is not None:
        return explicit
    return DEFAULT_REVIEW_CONFIG if DEFAULT_REVIEW_CONFIG.is_file() else None


def _editor_mutation_blocked(state_dir: Path | None) -> bool:
    if state_dir is None or not weekly_run_in_progress(state_dir):
        return False
    print("reason_code=weekly_run_in_progress")
    print("Идёт еженедельный поиск. Изменение не сохранено — повторите после его завершения.")
    return True


def _validate_iso_date(value: str) -> str:
    """Validate --run-date is YYYY-MM-DD at argparse parse time."""
    from datetime import date as _date

    try:
        _date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid ISO date '{value}'. Expected format: YYYY-MM-DD")
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise argparse.ArgumentTypeError(f"Invalid ISO date '{value}'. Expected format: YYYY-MM-DD")
    return value
