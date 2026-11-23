"""Replay Candidate History identity keys under today's admission rules (Issue #854).

Admission rules decide which people a mention names before its identity key
is computed. A change to those rules must not move the key of a book that is
already in Candidate History, or the next sighting becomes a second identity.

This check admits every history record's stored title and primary authors
again, exactly as a new Source Mention would be admitted, recomputes the
identity key, and reports every record whose recomputed key differs from its
stored key. It is read-only.

Run it before and after an admission change: keys that differ in both runs
were stored under older rules, and only keys that newly differ come from the
change.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from autoscout.identity import normalized_identity_key
from autoscout.source_mentions import with_split_author_lists
from autoscout.state_store import Collection
from autoscout.state_store import open_state_store

DEFAULT_STATE_DIR = Path("runs/current-weekly-pilot-russian")


def replayed_identity_key(record: dict[str, Any]) -> str | None:
    """Return the key a fresh sighting of this record would get, or None.

    None means the record stores no title or no author list to replay.
    """
    title = str(record.get("title", "") or "").strip()
    authors = record.get("primary_authors")
    if not title or not isinstance(authors, list) or not any(str(author).strip() for author in authors):
        return None
    admitted = with_split_author_lists({"title": title, "primary_authors": list(authors)})
    return normalized_identity_key(admitted)


def replay_history(history: list[Any]) -> dict[str, Any]:
    moved: list[dict[str, Any]] = []
    not_replayable: list[str] = []
    replayed = 0
    for record in history:
        if not isinstance(record, dict):
            continue
        stored = str(record.get("identity_key", "")).strip()
        key = replayed_identity_key(record)
        if key is None:
            not_replayable.append(stored)
            continue
        replayed += 1
        if key != stored:
            moved.append(
                {
                    "stored_identity_key": stored,
                    "replayed_identity_key": key,
                    "primary_authors": list(record.get("primary_authors", [])),
                }
            )
    moved.sort(key=lambda item: item["stored_identity_key"])
    return {
        "record_count": len(history),
        "replayed_count": replayed,
        "moved_count": len(moved),
        "moved": moved,
        "not_replayable": sorted(not_replayable),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--state-dir", type=Path, default=DEFAULT_STATE_DIR, help="State directory whose Candidate History is replayed."
    )
    args = parser.parse_args(argv)
    history = open_state_store(args.state_dir).read(Collection.CANDIDATE_HISTORY)
    if not isinstance(history, list):
        print("candidate history must be a list", file=sys.stderr)
        return 2
    print(json.dumps(replay_history(history), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
