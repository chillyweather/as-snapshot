"""The repository root the running code reads its configs and data from (#1159).

A module that needs the checkout's own files (``configs/``, ``data/``,
``runs/``) asks this function instead of deriving the root from its own
``__file__``. Normally both are the same directory. When new code fails the
run server's gate, the night runs the last code that passed from a snapshot
under ``data/code-gate/`` (see ``autoscout.code_gate_fallback``); that
snapshot holds only the package, so the run names the production checkout in
``AUTOSCOUT_REPOSITORY_ROOT`` and every module reads the checkout's configs,
state database and data from there.
"""

from __future__ import annotations

import os
from pathlib import Path

REPOSITORY_ROOT_ENV = "AUTOSCOUT_REPOSITORY_ROOT"


def repository_root() -> Path:
    """``AUTOSCOUT_REPOSITORY_ROOT`` when set, else the checkout that holds this package."""
    configured = os.environ.get(REPOSITORY_ROOT_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def code_root() -> Path:
    """The directory that holds the running package: where its code is, not where its configs and data are.

    The two differ only in a run on the last passing code, whose package is a
    snapshot. Use it to name a location in the code (a traceback frame), never
    to find configs, data or state.
    """
    return Path(__file__).resolve().parents[1]
