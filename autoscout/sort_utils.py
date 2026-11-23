"""Deterministic sorting helpers for persisted JSON records."""

from __future__ import annotations

import json
from typing import Any


def sort_by_key(records: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Sort records by a primary key plus canonical JSON tie-breaker."""
    return sorted(
        records,
        key=lambda record: (
            str(record.get(key, "")),
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ),
    )
