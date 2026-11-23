"""Convert a state directory's Candidate History from the single file to one file per book (#1073).

The conversion is explicit and runs once per state directory: it reads
``candidate-history.json``, validates every record, writes the shards into a
staging directory, reads them back and checks that the records equal the
originals, and only then moves the shards into place and removes the single
file. A failure at any step leaves the state directory as it was. Nothing
else in AutoScout converts a directory.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections import Counter
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoscout.json_io import serialize_json
from autoscout.sort_utils import sort_by_key
from autoscout.state_store import StateStoreError
from autoscout.state_store.layout import CANDIDATE_HISTORY_DIR
from autoscout.state_store.layout import CANDIDATE_HISTORY_FILE
from autoscout.state_store.layout import read_sharded_records
from autoscout.state_store.layout import shard_contents
from autoscout.state_store.layout import shard_relative_path


class ConversionError(StateStoreError):
    """The conversion refused the state directory; nothing was changed."""


@dataclass(frozen=True)
class ConversionReport:
    status: str
    record_count: int
    shard_count: int
    directory_count: int
    largest_directory_count: int
    total_shard_bytes: int
    single_file_bytes: int
    # Whether the records read back from the shards serialize to the single
    # file's exact bytes: true for a file the State Store wrote.
    single_file_bytes_reproduced: bool
    replaced_existing_shards: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_single_file(state_dir: Path) -> tuple[bytes, list[Any]]:
    single = state_dir / CANDIDATE_HISTORY_FILE
    if single.is_symlink():
        raise ConversionError(f"{CANDIDATE_HISTORY_FILE} must not be a symlink")
    if not single.is_file():
        raise ConversionError(f"no {CANDIDATE_HISTORY_FILE} to convert in the state directory")
    raw = single.read_bytes()
    try:
        records = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConversionError(f"{CANDIDATE_HISTORY_FILE} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(records, list):
        raise ConversionError(f"{CANDIDATE_HISTORY_FILE} must hold a JSON list of records")
    return raw, records


def convert_to_sharded(state_dir: Path, *, dry_run: bool = False, force: bool = False) -> ConversionReport:
    """Convert *state_dir* to the sharded layout, or with *dry_run* report what the conversion would write.

    A state directory that already has a ``candidate-history/`` directory is
    refused unless *force* is given; with it, the existing shards are
    replaced by the single file's records.
    """
    shard_root = state_dir / CANDIDATE_HISTORY_DIR
    has_shards = shard_root.exists() or shard_root.is_symlink()
    if has_shards and not force:
        raise ConversionError(
            f"the state directory already has {CANDIDATE_HISTORY_DIR}/; pass --force to replace it "
            f"with the records of {CANDIDATE_HISTORY_FILE}"
        )
    if shard_root.is_symlink() or (has_shards and not shard_root.is_dir()):
        raise ConversionError(f"{CANDIDATE_HISTORY_DIR} in the state directory must be a real directory")
    raw, records = _load_single_file(state_dir)
    try:
        contents = shard_contents(records)
    except StateStoreError as exc:
        raise ConversionError(str(exc)) from exc
    relative_paths = {candidate_id: shard_relative_path(candidate_id) for candidate_id in contents}
    per_directory = Counter(path.parent for path in relative_paths.values())
    encoded = {candidate_id: content.encode("utf-8") for candidate_id, content in contents.items()}
    expected = sort_by_key(records, "identity_key")

    def report(status: str, reproduced: bool) -> ConversionReport:
        return ConversionReport(
            status=status,
            record_count=len(records),
            shard_count=len(contents),
            directory_count=len(per_directory),
            largest_directory_count=max(per_directory.values(), default=0),
            total_shard_bytes=sum(len(content) for content in encoded.values()),
            single_file_bytes=len(raw),
            single_file_bytes_reproduced=reproduced,
            replaced_existing_shards=has_shards and not dry_run,
        )

    if dry_run:
        return report("dry_run", serialize_json(expected).encode("utf-8") == raw)

    staging = Path(tempfile.mkdtemp(prefix=".candidate-history-conversion-", dir=state_dir))
    previous = staging / "previous"
    moved_in = False
    try:
        staged_root = staging / CANDIDATE_HISTORY_DIR
        for candidate_id, relative in relative_paths.items():
            path = staging.joinpath(*relative.parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(encoded[candidate_id])
        read_back = read_sharded_records(staging)
        if read_back != expected or len(read_back) != len(records):
            raise ConversionError("the records read back from the shards differ from the originals; nothing changed")
        if has_shards:
            os.rename(shard_root, previous)
        try:
            if staged_root.exists():
                os.rename(staged_root, shard_root)
                moved_in = True
            (state_dir / CANDIDATE_HISTORY_FILE).unlink()
        except BaseException:
            if moved_in:
                os.rename(shard_root, staged_root)
            if has_shards:
                os.rename(previous, shard_root)
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return report("converted", serialize_json(read_back).encode("utf-8") == raw)
