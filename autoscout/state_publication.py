"""Publish run files together with a State Store transaction (#1069, #1071).

A stage that changes canonical state and also rewrites files of its own run
(the current candidates, the run copy of Candidate History, a stage report)
must land both or neither. The files are published as one bundle inside the
store transaction, so a failed publish writes no state; when the state commit
fails after the files were published, the files are put back from versions
kept before the publish. Putting them back renames those versions, so it
allocates nothing and cannot fail for lack of space.
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path

from autoscout.state_store import StateStore


def prepare_rollback(paths: Sequence[Path]) -> dict[Path, Path | None]:
    """Keep each existing file's current version under a second name, before anything is published.

    The copy is a hard link where the file system allows one, so it needs no
    new space; a file that does not exist yet maps to ``None``.
    """
    rollback: dict[Path, Path | None] = {}
    try:
        for path in paths:
            if not path.exists():
                rollback[path] = None
                continue
            kept = path.with_name(f".{path.name}.{uuid.uuid4().hex}.rollback")
            try:
                os.link(path, kept)
            except OSError:
                shutil.copyfile(path, kept)
            rollback[path] = kept
    except BaseException:
        discard_rollback(rollback)
        raise
    return rollback


def roll_back(rollback: dict[Path, Path | None]) -> None:
    """Put the kept versions back by renaming them, which allocates nothing; remove files that were new."""
    for path, kept in rollback.items():
        if kept is None:
            path.unlink(missing_ok=True)
        else:
            os.replace(kept, path)


def discard_rollback(rollback: dict[Path, Path | None]) -> None:
    for kept in rollback.values():
        if kept is not None:
            kept.unlink(missing_ok=True)


def publish_files_with_state(
    store: StateStore,
    write_state: Callable[[], None],
    publish_files: Callable[[], None],
    destinations: Sequence[Path],
) -> None:
    """Run *write_state* and *publish_files* in one store transaction; the files follow the state.

    *write_state* stages the canonical writes, and *publish_files* publishes
    the run's files at *destinations*. An exception from either writes no
    state; a failed state commit after the files were published restores
    them as they were.
    """
    rollback = prepare_rollback(destinations)
    published = False
    try:
        with store.transaction():
            write_state()
            publish_files()
            published = True
    except BaseException:
        if published:
            roll_back(rollback)
        else:
            discard_rollback(rollback)
        raise
    discard_rollback(rollback)
