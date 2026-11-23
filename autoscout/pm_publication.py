"""PM-only candidates wait for their publication season (Issue #947).

A Publishers Marketplace deal announces a book one to three years before it
is published, so no page describes it yet. A candidate whose only evidence is
a PM ``rights/deal`` mention therefore skips both description stages, the
Brave grounded search and the Codex adjudication, until a re-check date taken
from the deal's expected publication.

Nothing is stored for this. The PM mention already carries the deal's
publication on the candidate's ``source_evidence`` when it is ingested, and
both stages read the same evidence, so the decision is recomputed from the
record every time. That is what makes early re-entry work in the same run: as
soon as another source's mention is on the record, the candidate is no longer
PM-only and goes back on the normal worklist, with no flag left to clear.

The module also owns the PM host check that keeps a PM dealmaker profile or
deal page from ever counting as grounding evidence for a description: the
page describes a deal, not the book.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

PM_HOST = "publishersmarketplace.com"
# The signal type every PM deal mention carries (pm_deal_capture.SIGNAL_TYPE,
# pinned equal by test; that module is not imported here to keep this one
# free of the capture's dependencies).
PM_SIGNAL_TYPE = "rights/deal"

# The month each publication season starts, as publishers' catalogues use them.
# Winter is read as the January that opens the stated year ("winter 2027" is
# January 2027): it is the earlier of the two readings once the lead below is
# applied, so a winter book is never searched late.
SEASON_START_MONTHS: Mapping[str, int] = {
    "winter": 1,
    "spring": 3,
    "summer": 6,
    "fall": 9,
}
# How far ahead of the season the search starts again: retailer and publisher
# pages usually appear a few months before publication, and two months leaves
# room for a page that comes late without searching a year early.
PUBLICATION_RECHECK_LEAD_MONTHS = 2
# A deal that names no publication at all is re-checked this many days after
# it was reported. PM deals are one to three years from publication; one year
# is the near end of that range, so a book is never searched later than its
# earliest plausible publication, while it still saves the roughly 25 searches
# a year the fixed 14-day re-check would spend on it.
DEFAULT_PUBLICATION_RECHECK_DAYS = 365


def is_pm_url(url: Any) -> bool:
    """True when *url* is a Publishers Marketplace page (any subdomain)."""
    try:
        host = (urlsplit(str(url or "").strip()).hostname or "").lower()
    except ValueError:
        return False
    return host == PM_HOST or host.endswith("." + PM_HOST)


def is_pm_evidence(entry: Any) -> bool:
    """True when one ``source_evidence`` entry is a PM rights/deal mention."""
    if not isinstance(entry, Mapping):
        return False
    return str(entry.get("signal_type", "")).strip() == PM_SIGNAL_TYPE and is_pm_url(entry.get("source_url"))


def is_pm_deal_evidence(entry: Any) -> bool:
    """True when one ``source_evidence`` entry is a captured PM deal announcement.

    Only the deals capture (#945) writes ``rights_deal``. A PM rights offering
    shares the signal type and host but announces no sale, so it says nothing
    about a coming publication and never puts a book on hold.
    """
    return is_pm_evidence(entry) and isinstance(entry.get("rights_deal"), Mapping)


# Candidate History remembers that some source other than a PM deal cited the
# book. The weekly snapshot keeps only the latest week's evidence, so without
# it a book Kirkus listed last week would read as PM-only when this week only
# PM reports its deal. Set once, never cleared.
CITED_BEYOND_PM_DEALS_FIELD = "cited_beyond_pm_deals"


# The earliest re-check date any PM deal for the book has given, kept on
# Candidate History. A later deal for the same book replaces the weekly
# evidence snapshot, and must never push the re-check later than a season
# an earlier deal already named.
PM_RECHECK_DATE_FIELD = "pm_publication_recheck_date"


def cited_beyond_pm_deals(evidence: Any) -> bool:
    """True when some entry of *evidence* is not a captured PM deal announcement."""
    return isinstance(evidence, list) and any(not is_pm_deal_evidence(entry) for entry in evidence)


def is_pm_only(record: Mapping[str, Any]) -> bool:
    """True when every source that ever cited *record* is a captured PM deal announcement."""
    if record.get(CITED_BEYOND_PM_DEALS_FIELD) is True:
        return False
    evidence = record.get("source_evidence")
    if not isinstance(evidence, list) or not evidence:
        return False
    return not cited_beyond_pm_deals(evidence)


# Rights offerings were captured with a second signal type and with relative
# links before the capture settled, so a lead is told by its signal type alone.
RIGHTS_LEAD_SIGNAL_TYPES = frozenset({PM_SIGNAL_TYPE, "rights_offering"})


def is_rights_lead_only(record: Mapping[str, Any]) -> bool:
    """True when the only thing known about *record* is a rights mention.

    A rights mention is a deal announcement or a rights offering. Such a book
    is a lead to follow, not a book to assess: it is usually unpublished and
    has no description. A book that carries only captured PM deals follows
    ``is_pm_only``, so one that another source cited in an earlier week is not
    a lead.
    """
    evidence = record.get("source_evidence")
    if not isinstance(evidence, list) or not evidence:
        return False
    for entry in evidence:
        if not isinstance(entry, Mapping) or str(entry.get("signal_type", "")).strip() not in RIGHTS_LEAD_SIGNAL_TYPES:
            return False
    if all(is_pm_deal_evidence(entry) for entry in evidence):
        return is_pm_only(record)
    return True


def _months_before(year: int, month: int, months: int) -> date:
    index = year * 12 + (month - 1) - months
    return date(index // 12, index % 12 + 1, 1)


def season_recheck_date(publication: Any) -> date | None:
    """The re-check date for one deal's ``publication``, or ``None`` when it names none.

    The search starts again ``PUBLICATION_RECHECK_LEAD_MONTHS`` before the
    first day of the stated month or season. A deal that names only a year is
    read from that year's January, the earliest it could mean.
    """
    if not isinstance(publication, Mapping):
        return None
    year = publication.get("year")
    if type(year) is not int or year < 1:
        return None
    month = publication.get("month")
    season = str(publication.get("season", "") or "").strip().casefold()
    if type(month) is int and 1 <= month <= 12:
        start_month = month
    elif season in SEASON_START_MONTHS:
        start_month = SEASON_START_MONTHS[season]
    else:
        start_month = 1
    return _months_before(year, start_month, PUBLICATION_RECHECK_LEAD_MONTHS)


def _reported_date(entry: Mapping[str, Any], record: Mapping[str, Any]) -> date | None:
    for raw in (entry.get("source_check_date"), record.get("first_seen_date")):
        try:
            return date.fromisoformat(str(raw or "").strip())
        except ValueError:
            continue
    return None


def pm_publication_recheck_date(record: Mapping[str, Any]) -> date | None:
    """The date a PM-only record goes back to description search, or ``None``.

    ``None`` when the record is not PM-only. Each PM deal gives the date from
    its season (``season_recheck_date``), or ``DEFAULT_PUBLICATION_RECHECK_DAYS``
    after it was reported when it names no publication. When several deals
    cite the book, the earliest date wins, so the book is never searched late;
    that includes a date an earlier week's deal left in ``PM_RECHECK_DATE_FIELD``.
    A record with neither a publication nor a report date has no date and is
    searched as usual.
    """
    if not is_pm_only(record):
        return None
    dates: list[date] = []
    for entry in record.get("source_evidence", []):
        deal = entry.get("rights_deal")
        from_season = season_recheck_date(deal.get("publication") if isinstance(deal, Mapping) else None)
        if from_season is not None:
            dates.append(from_season)
            continue
        reported = _reported_date(entry, record)
        if reported is not None:
            dates.append(reported + timedelta(days=DEFAULT_PUBLICATION_RECHECK_DAYS))
    try:
        dates.append(date.fromisoformat(str(record.get(PM_RECHECK_DATE_FIELD, "") or "").strip()))
    except ValueError:
        pass
    return min(dates) if dates else None


def pm_publication_recheck_pending(record: Mapping[str, Any], as_of: date) -> bool:
    """True while a PM-only record waits for its re-check date."""
    recheck = pm_publication_recheck_date(record)
    return recheck is not None and as_of < recheck
