"""One wall-clock deadline for the capture of a single configured source.

The capture stage clamps every source to a per-source allowance.  That value
used to be handed to the HTTP layer as a per-socket-operation timeout, so a
source with a declared detail route could issue one request per row and spend
``(1 + max_pages) x timeout`` seconds against an allowance of ``timeout``
(issue #717).  A deadline makes the declared allowance mean what it says: it
is computed once when the source starts and bounds every request that source
makes afterwards.

The deadline carries its own monotonic clock so the capture worklist's
injected clock reaches the fetch helpers unchanged, and so nothing below the
worklist has to read a clock of its own.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# The private source key that carries one source's deadline from the capture
# worklist to the bounded fetch helpers.  It is stripped from every published
# artifact by construction: only the worklist's private per-source copy of a
# configured source ever holds it.
CAPTURE_DEADLINE_KEY = "_capture_deadline"


@dataclass(frozen=True)
class CaptureDeadline:
    """The wall-clock bound one source's capture must finish inside."""

    allowance_seconds: float
    expires_at: float
    monotonic: Callable[[], float]

    @classmethod
    def starting_at(cls, started: float, allowance_seconds: float, monotonic: Callable[[], float]) -> CaptureDeadline:
        """Return the deadline of a source that started at *started*."""
        return cls(allowance_seconds=allowance_seconds, expires_at=started + allowance_seconds, monotonic=monotonic)

    def remaining_seconds(self) -> float:
        """Return the time this source has left, which may be negative."""
        return self.expires_at - self.monotonic()

    def exhausted(self) -> bool:
        """Return whether this source has spent its whole allowance."""
        return self.remaining_seconds() <= 0

    @property
    def exhausted_reason(self) -> str:
        """Return the failure reason, which names the bound that was hit."""
        return f"capture per-source bound exhausted after {self.allowance_seconds:.1f}s"


def capture_deadline_for(source: Mapping[str, Any]) -> CaptureDeadline | None:
    """Return the deadline carried by one source, when the caller set one.

    A source fetched outside the capture worklist - a probe, a feasibility
    check, a replay - carries no deadline and keeps its socket timeout as its
    only bound.
    """
    deadline = source.get(CAPTURE_DEADLINE_KEY)
    return deadline if isinstance(deadline, CaptureDeadline) else None
