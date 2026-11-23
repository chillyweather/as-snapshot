"""Identity values used to decide whether a stored pipeline result is reusable."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RunIdentity:
    """The operation and evidence stamps for one pipeline invocation."""

    operation_id: str = ""
    evidence_digest: str = ""


def stamps_permit_short_circuit(current: RunIdentity, stored: RunIdentity) -> bool:
    """Return whether the stored report answers the current invocation.

    The decision fails closed when either side has a stamp that the other side
    cannot match.
    """
    stamps = (
        (current.operation_id, stored.operation_id),
        (current.evidence_digest, stored.evidence_digest),
    )
    if any(
        bool(current_value or stored_value) and current_value != stored_value for current_value, stored_value in stamps
    ):
        return False
    if (current.operation_id or stored.operation_id) and not (current.evidence_digest and stored.evidence_digest):
        return False
    return True
