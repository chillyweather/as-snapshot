"""Shared vendor request throttling."""

from __future__ import annotations

import threading
import time
from typing import Any

# Brave's free and credit tiers allow one query per second.
MIN_VENDOR_INTERVAL_SECONDS = 1.0


class VendorRateLimiter:
    """Space shared vendor calls by a minimum interval.

    The clock and sleeper are injected so tests can prove spacing without real
    waiting.
    The lock makes one instance safe for concurrent callers.
    """

    def __init__(
        self,
        min_interval_seconds: float = MIN_VENDOR_INTERVAL_SECONDS,
        *,
        monotonic: Any = time.monotonic,
        sleeper: Any = time.sleep,
    ) -> None:
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._last_started_at: float | None = None

    def wait(self, deadline: float | None = None) -> float | None:
        """Wait for the next call, or return ``None`` when it cannot fit.

        ``deadline`` is an absolute monotonic timestamp.
        A queued wait is rejected before sleeping when its scheduled start is
        at or after the deadline.
        If an injected sleeper overshoots the deadline, the call is rejected
        and the overshot time remains the limiter's last scheduled start.
        Calls without a deadline retain the ordinary blocking behavior.
        """
        with self._lock:
            now = float(self._monotonic())
            if deadline is not None and now >= float(deadline):
                return None
            if self._last_started_at is None:
                self._last_started_at = now
                return 0.0

            earliest = self._last_started_at + self.min_interval_seconds
            delay = earliest - now
            if delay <= 0.0:
                self._last_started_at = now
                return 0.0
            if deadline is not None and earliest >= float(deadline):
                return None

            self._sleeper(delay)
            after_sleep = float(self._monotonic())
            scheduled_start = max(earliest, after_sleep)
            self._last_started_at = scheduled_start
            if deadline is not None and scheduled_start >= float(deadline):
                return None
            return float(delay)


__all__ = ["MIN_VENDOR_INTERVAL_SECONDS", "VendorRateLimiter"]
