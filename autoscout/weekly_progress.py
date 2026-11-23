"""One heartbeat for the main weekly stage and its background Kirkus pass."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from autoscout.json_io import write_json
from autoscout.progress_snapshot import PROGRESS_HEARTBEAT_INTERVAL_SECONDS
from autoscout.progress_snapshot import PROGRESS_SNAPSHOT_INTERVAL_SECONDS
from autoscout.progress_snapshot import ProgressSnapshotFailureRecorder
from autoscout.progress_snapshot import ProgressSnapshotPresentation
from autoscout.progress_snapshot import summarize_snapshot_rows

KIRKUS_PROGRESS_SNAPSHOT_FILE = "kirkus-progress-snapshot.json"
KIRKUS_WAIT_STAGE = "kirkus-wait"


class WeeklyProgressPresentation:
    """Observe every writer update; serialize persistence and presentation.

    Main writers keep ownership of progress-snapshot.json. Only this object
    writes the Kirkus sidecar, including its terminal status, so doctor can
    read both without a read/modify/write race on the main snapshot.
    """

    def __init__(
        self,
        output_dir: Path,
        presentation: ProgressSnapshotPresentation | None,
        *,
        monotonic: Any = time.monotonic,
        failure_recorder: ProgressSnapshotFailureRecorder | None = None,
    ) -> None:
        self._path = output_dir / KIRKUS_PROGRESS_SNAPSHOT_FILE
        self._presentation = presentation
        self._monotonic = monotonic
        self._failure_recorder = failure_recorder
        self._write_failed = False
        self._lock = threading.Lock()
        self._main: dict[str, Any] | None = None
        self._kirkus: dict[str, Any] | None = None
        self._closed = False
        self._last_presented: float | None = None
        self._last_attempt: float | None = None

    def __call__(self, snapshot: dict[str, Any]) -> None:
        self.update_snapshot(snapshot)

    def start_kirkus(self, budget_seconds: float) -> None:
        self.update_snapshot(
            {
                "stage": "kirkus-precollection",
                "status": "running",
                "elapsed_seconds": 0.0,
                "stage_budget_seconds": budget_seconds,
                "rows": [],
            }
        )

    def finish_kirkus(self) -> None:
        with self._lock:
            self._closed = True
            if self._kirkus is not None:
                self._kirkus = {**self._kirkus, "status": "finished"}
                self._persist_kirkus()

    def _persist_kirkus(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            write_json(self._path, self._kirkus)
        except Exception as exc:
            if not self._write_failed and self._failure_recorder is not None:
                self._write_failed = True
                self._failure_recorder("kirkus-precollection", f"{type(exc).__name__}: {exc}")

    def update_snapshot(self, snapshot: dict[str, Any]) -> None:
        with self._lock:
            if snapshot.get("stage") == "kirkus-precollection":
                if self._closed:
                    return
                self._kirkus = snapshot
                self._persist_kirkus()
            else:
                self._main = snapshot
            # A pass starting before capture has no main-stage counts yet.
            if self._main is None or self._main.get("status") != "running":
                return
            if self._main.get("stage") == KIRKUS_WAIT_STAGE:
                if self._kirkus is None or self._kirkus.get("status") != "running":
                    return
            elif summarize_snapshot_rows(self._main.get("rows", []))["running"] <= 0:
                return
            current = self._monotonic()
            if (
                self._last_presented is not None
                and current - self._last_presented < PROGRESS_HEARTBEAT_INTERVAL_SECONDS
            ):
                return
            if self._last_attempt is not None and current - self._last_attempt < PROGRESS_SNAPSHOT_INTERVAL_SECONDS:
                return
            if self._presentation is None:
                return
            payload = dict(self._main)
            if self._kirkus is not None and self._kirkus.get("status") == "running":
                payload["kirkus"] = self._kirkus
            self._last_attempt = current
            self._presentation(payload)
            self._last_presented = current
