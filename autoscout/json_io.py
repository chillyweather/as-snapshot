"""JSON read/write primitives — single authoritative implementation.

One place for the project's standard JSON serialization and
deserialisation helpers so that callers do not repeat the same
``json.loads(path.read_text(…))`` / ``json.dumps(…, …)`` patterns.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def read_json(path: Path, default: Any = None) -> Any:
    """Read and parse a JSON file.

    Returns *default* when *path* does not exist, allowing callers
    to skip an explicit ``.exists()`` guard.
    """
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def read_json_or_default(path: Path, default: Any) -> Any:
    """Read a JSON file or return *default* when it is missing.

    This is a convenience alias for ``read_json(path, default=default)``
    that preserves the calling convention used by the CLI entry-point.
    """
    return read_json(path, default)


def serialize_json(data: Any) -> str:
    """Return the canonical pretty-printed, key-sorted UTF-8 JSON form."""
    return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def write_json(path: Path, data: Any) -> None:
    """Atomically write *data* as pretty-printed, key-sorted UTF-8 JSON.

    The serialized form includes a trailing newline, which is the project's
    canonical JSON representation for persisted state and artifacts.
    """
    _write_text_atomically(path, serialize_json(data))


def write_json_preserving_key_order(path: Path, data: Any) -> None:
    """Atomically write *data* as indent-2 UTF-8 JSON, keeping its own key order.

    For hand-ordered files such as source configs, where re-sorting every
    object would turn a small edit into a diff of the whole file (#1111).
    Key order is kept; layout is normalized to ``json.dumps(indent=2)``, so
    only a file already in that layout changes in nothing but the edited keys.
    """
    _write_text_atomically(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _write_text_atomically(path: Path, serialized: str) -> None:
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_name = tmp.name
            tmp.write(serialized)
        os.replace(tmp_name, path)
    finally:
        if tmp_name and os.path.exists(tmp_name):
            os.unlink(tmp_name)
