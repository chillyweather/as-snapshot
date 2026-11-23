"""Freshness for a recurring ranking whose page carries no date.

A dated ranking proves its own currency: the page prints a reporting period,
``validate_ranking_contract_metadata`` requires a pattern, a date format, and a
maximum age, and ``ranking_page_identity`` rejects a period that is in the
future or older than the declared window.

A recurring list that names only its cadence, for example ``de la semana``,
can prove none of that.  There is no period text to match, so a declared
pattern would be fiction and a declared maximum age would be a lie dressed as
a declaration.  Such a source declares ``ranking_reporting_period_undated``
instead, and proves currency by movement: the bounded rows of one run are
fingerprinted, and a run whose fingerprint repeats the previous run is a frozen
list rather than a current one.

Four rules keep that honest:

* The fingerprint covers the whole bounded row set in page order, so a source
  cannot pass by changing one row while the list is stale.
* An unchanged fingerprint is never re-emitted as a current signal.  A
  frozen list is reported as failed.
* Freshness state is required.  Without a retained previous fingerprint an
  undated source cannot prove movement, so it fails closed as well.  The
  source's first accepted observation has no previous fingerprint, so it is
  allowed through and records the fingerprint it observed.
* An unchanged list is judged against its declared cadence.  A weekly list
  run again two days later has simply not published its next edition yet:
  that run is skipped, not failed, and still re-emits nothing.  Only a list
  unchanged for its whole cadence or longer is frozen.  A source without a
  known cadence keeps the strict rule.
* Only an accepted run records what it observed.  The retained fingerprint
  asserts that this was the current list on that date, and a run that failed
  proved nothing of the kind.  Retaining it for a failed run froze the source:
  the next run met the same unchanged page and was refused for a reason it
  could not clear (issue #716).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from autoscout.source_metadata import RANKING_UNDATED_FRESHNESS_FIELD

RANKING_DATED_FRESHNESS_FIELDS: tuple[str, ...] = (
    "ranking_reporting_period_pattern",
    "ranking_reporting_period_date_format",
    "ranking_reporting_period_max_age_days",
)

FROZEN_FAILURE_STATUS = "ranking_list_frozen"
NOT_YET_REFRESHED_SKIP_STATUS = "ranking_list_not_yet_refreshed"
MISSING_STATE_FAILURE_STATUS = "missing_undated_freshness_state"

FINGERPRINT_DEBUG_FIELD = "ranking_row_fingerprint"


# The longest gap between two editions of a list with this cadence.
_CADENCE_DAYS = {"daily": 1, "weekly": 7, "monthly": 31, "annual": 366}


def _cadence_days(source: Mapping[str, Any]) -> int:
    """Return the declared cadence in days, or 0 when the source declares none this module knows."""
    for field in ("check_frequency", "refresh_cadence"):
        days = _CADENCE_DAYS.get(str(source.get(field, "")).strip().casefold(), 0)
        if days:
            return days
    return 0


def _days_between(earlier: str, later: str) -> int | None:
    try:
        return (date.fromisoformat(later) - date.fromisoformat(earlier)).days
    except ValueError:
        return None


def freshness_status_is_skip(status: str) -> bool:
    """Return whether an undated-freshness verdict skips the source rather than failing it."""
    return status == NOT_YET_REFRESHED_SKIP_STATUS


def ranking_undated_declared(source: Mapping[str, Any]) -> bool:
    """Return whether a ranking source declares an undated recurring page."""
    return source.get(RANKING_UNDATED_FRESHNESS_FIELD) is True


def validate_undated_freshness_declaration(source: Mapping[str, Any]) -> str:
    """Return the declaration error for undated ranking freshness, if any."""
    value = source.get(RANKING_UNDATED_FRESHNESS_FIELD)
    if value is None:
        return ""
    if value is not True and value is not False:
        return f"{RANKING_UNDATED_FRESHNESS_FIELD} must be true or false"
    if value is False:
        return ""
    declared = [
        field
        for field in (*RANKING_DATED_FRESHNESS_FIELDS, "ranking_reporting_date_selector")
        if str(source.get(field, "")).strip()
    ]
    if declared:
        return (
            f"{RANKING_UNDATED_FRESHNESS_FIELD} cannot be combined with dated reporting metadata: {', '.join(declared)}"
        )
    return ""


def ranking_freshness_state_key(source: Mapping[str, Any]) -> str:
    """Return one stable state key derived from configured source identity."""
    identity = f"{source.get('name', '')}\x1f{source.get('url', '')}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def ranking_row_fingerprint(rows: Iterable[Any]) -> str:
    """Return one deterministic fingerprint of the bounded rows in page order.

    The fingerprint covers rank, title, author, and the row identifier of every
    bounded row, in the order the page presents them.  It deliberately covers
    the whole set: a stale list that gains one new row is still a stale list,
    and a fingerprint over a sample would hide that.
    """
    payload = [
        [
            str(getattr(row, "rank", "")),
            str(getattr(row, "title", "")),
            str(getattr(row, "author", "")),
            str(getattr(row, "identifier", "")),
        ]
        for row in rows
    ]
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RankingFreshnessRecord:
    """One retained undated-ranking observation for one configured source."""

    state_key: str
    source_name: str
    fingerprint: str
    first_seen: str
    last_changed: str

    def to_dict(self) -> dict[str, str]:
        return {
            "state_key": self.state_key,
            "source_name": self.source_name,
            "fingerprint": self.fingerprint,
            "first_seen": self.first_seen,
            "last_changed": self.last_changed,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RankingFreshnessRecord | None:
        state_key = str(value.get("state_key", "")).strip()
        fingerprint = str(value.get("fingerprint", "")).strip()
        if not state_key or not fingerprint:
            return None
        return cls(
            state_key=state_key,
            source_name=str(value.get("source_name", "")).strip(),
            fingerprint=fingerprint,
            first_seen=str(value.get("first_seen", "")).strip(),
            last_changed=str(value.get("last_changed", "")).strip(),
        )


@dataclass(frozen=True)
class RankingFreshnessLedger:
    """The retained undated-ranking fingerprint of every configured source."""

    records: tuple[RankingFreshnessRecord, ...] = ()

    @classmethod
    def from_records(cls, values: Any) -> RankingFreshnessLedger:
        if not isinstance(values, list):
            return cls()
        parsed = [RankingFreshnessRecord.from_dict(value) for value in values if isinstance(value, Mapping)]
        by_key = {record.state_key: record for record in parsed if record is not None}
        return cls(tuple(sorted(by_key.values(), key=lambda record: record.state_key)))

    def record_for(self, state_key: str) -> RankingFreshnessRecord | None:
        return next((record for record in self.records if record.state_key == state_key), None)

    def observe(self, source: Mapping[str, Any], fingerprint: str, run_date: str) -> RankingFreshnessLedger:
        """Return the ledger that retains one observed fingerprint."""
        state_key = ranking_freshness_state_key(source)
        previous = self.record_for(state_key)
        first_seen = previous.first_seen if previous is not None and previous.first_seen else run_date
        updated = RankingFreshnessRecord(
            state_key=state_key,
            source_name=str(source.get("name", "")),
            fingerprint=fingerprint,
            first_seen=first_seen,
            last_changed=run_date,
        )
        remaining = [record for record in self.records if record.state_key != state_key]
        return RankingFreshnessLedger(tuple(sorted([*remaining, updated], key=lambda record: record.state_key)))

    def to_records(self) -> list[dict[str, str]]:
        return [record.to_dict() for record in self.records]


def undated_freshness_failure(
    source: Mapping[str, Any],
    fingerprint: str,
    ledger: RankingFreshnessLedger | None,
    run_date: str = "",
) -> tuple[str, str]:
    """Return the fail-closed reason and status for one undated ranking run.

    An empty reason means the run may proceed.  A status for which
    ``freshness_status_is_skip`` holds means the list is unchanged within its
    cadence on *run_date*: the run is skipped and reports nothing.  The caller is responsible for
    retaining the observed fingerprint through ``RankingFreshnessLedger.observe``
    once the run is accepted -- that is, only after the Source Run Status is
    known and says the run collected.  A run that proceeded past this check and
    then failed for any other reason retains nothing.
    """
    if not ranking_undated_declared(source):
        return "", ""
    if not fingerprint:
        return (
            "undated ranking list produced no bounded row fingerprint",
            MISSING_STATE_FAILURE_STATUS,
        )
    if ledger is None:
        return (
            "undated ranking list requires retained freshness state to prove movement",
            MISSING_STATE_FAILURE_STATUS,
        )
    previous = ledger.record_for(ranking_freshness_state_key(source))
    if previous is None:
        return "", ""
    if previous.fingerprint == fingerprint:
        since = previous.last_changed or previous.first_seen
        cadence = _cadence_days(source)
        age = _days_between(since, run_date) if since and run_date else None
        if cadence and age is not None and 0 <= age < cadence:
            return (
                f"undated ranking list has not been refreshed since {since}, within its {cadence}-day cadence",
                NOT_YET_REFRESHED_SKIP_STATUS,
            )
        suffix = f" since {since}" if since else ""
        return (
            f"undated ranking list is unchanged{suffix} and cannot be reported as current",
            FROZEN_FAILURE_STATUS,
        )
    return "", ""
