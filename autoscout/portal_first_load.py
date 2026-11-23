"""The one manual first load of the booksellers' portal (issue #1140).

CEGAL's permission allows one first load of a few hundred titles beside the
few dozen weekly lookups. This pass asks the portal about the books already
in Candidate History that a weekly pass would ask about: no editor decision,
found only on non-English lists, one of them Spanish, with an ISBN the
portal ledger does not hold yet. It counts against the first-load ceiling
(:data:`autoscout.bookseller_portal.FIRST_LOAD_CEILING` in total), never the
weekly one.

It writes only the portal ledger. The answers are used, without a new
request, by the next original-edition pass that meets the book
(``./scripts/complete-originals`` or a weekly run). It calls no model.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.bookseller_portal import MODE_FIRST_LOAD
from autoscout.bookseller_portal import PortalLedgerError
from autoscout.bookseller_portal import PortalLookup
from autoscout.isbn_completion import isbn13
from autoscout.learned_field_completion import SCOPE_UNDECIDED
from autoscout.learned_field_completion import read_history
from autoscout.learned_field_completion import select_records
from autoscout.original_edition import STATUS_CONFIRMED
from autoscout.original_edition import validate_original_edition
from autoscout.state_store import open_state_store
from autoscout.title_evidence import record_has_only_non_english_sources

# A progress line is printed after this many requests.
PROGRESS_EVERY = 25


def _languages(record: Mapping[str, Any]) -> set[str]:
    evidence = record.get("source_evidence")
    if not isinstance(evidence, list):
        return set()
    return {str(entry.get("language", "") or "").strip().casefold() for entry in evidence if isinstance(entry, Mapping)}


def _settled(record: Mapping[str, Any]) -> bool:
    """True when the portal could add nothing: a confirmed original and a page count are already there."""
    edition = record.get("original_edition")
    if not isinstance(edition, dict) or not validate_original_edition(edition):
        return False
    return edition.get("status") == STATUS_CONFIRMED and record.get("page_count") is not None


def first_load_isbns(history: Sequence[dict[str, Any]]) -> list[str]:
    """The distinct ISBN-13s worth a first-load lookup, in Candidate History order."""
    isbns: list[str] = []
    for record in select_records(list(history), SCOPE_UNDECIDED, [], {}):
        isbn = isbn13(record.get("isbn"))
        if not isbn or isbn in isbns or _settled(record):
            continue
        if "es" not in _languages(record) or not record_has_only_non_english_sources(record):
            continue
        isbns.append(isbn)
    return isbns


def run_portal_first_load(
    state_dir: Path,
    *,
    lookup: PortalLookup,
    apply: bool,
    limit: int | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Preview, or with ``apply`` make, the first-load lookups; counts only.

    ``lookup`` must be in first-load mode. Without ``apply`` nothing is asked
    or written.
    """
    if lookup.mode != MODE_FIRST_LOAD:
        raise ValueError("the first load runs in first-load mode")
    history = read_history(open_state_store(state_dir))
    if history is None:
        return {"status": "input_failed", "reason": "invalid_candidate_history"}
    try:
        kept = lookup.ledger.answers()
        remaining = lookup.ledger.remaining(MODE_FIRST_LOAD, lookup.today)
    except PortalLedgerError:
        return {"status": "input_failed", "reason": "portal_ledger_unreadable"}
    candidates = first_load_isbns(history)
    to_ask = [isbn for isbn in candidates if isbn not in kept]
    planned = min(len(to_ask), remaining, limit if limit is not None else len(to_ask))
    result: dict[str, Any] = {
        "status": "ok",
        "applied": apply,
        "candidates": len(candidates),
        "already_kept": len(candidates) - len(to_ask),
        "first_load_remaining": remaining,
        "planned": planned,
    }
    if not apply:
        return result
    for isbn in to_ask[:planned]:
        lookup.answer(isbn)
        if lookup.stats.ledger_unavailable or lookup.stats.ceiling_reached or lookup.stats.unavailable:
            break
        if progress is not None and lookup.stats.requests and lookup.stats.requests % PROGRESS_EVERY == 0:
            progress(lookup.stats.requests, planned)
    result["portal"] = lookup.stats.as_dict()
    try:
        result["first_load_remaining"] = lookup.ledger.remaining(MODE_FIRST_LOAD, lookup.today)
    except PortalLedgerError:
        result["first_load_remaining"] = 0
    return result


def first_load_lookup(state_dir: Path, fetch: Any, *, today: date | None = None) -> PortalLookup:
    """A first-load lookup over the ledger of ``state_dir``; ``fetch=None`` for a preview."""
    from autoscout.bookseller_portal import PortalLedger

    return PortalLookup(PortalLedger.in_state_dir(state_dir), fetch, mode=MODE_FIRST_LOAD, today=today or date.today())


__all__ = ["PROGRESS_EVERY", "first_load_isbns", "first_load_lookup", "run_portal_first_load"]
