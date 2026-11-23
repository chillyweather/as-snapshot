"""Source families whose capture is a list of validated rows, not page HTML.

Each family states in one place what the agent-extraction path needs from it:
how its rows are captured, the text the model reads, and the rules and fields
its extraction contract adds. ``ROW_FAMILIES`` is keyed by the name
``contract_family`` resolves, so the capture, the model input, the contract and
the prompt all read the same entry.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from autoscout import bookinfo_ranking as bookinfo
from autoscout import ipc_top40_ranking as ipc_top40
from autoscout import kirkus_listing as kirkus
from autoscout import perlentaucher_digest as perlentaucher
from autoscout.source_collectors.base import contract_family


@dataclass(frozen=True)
class RowCapture:
    """What one live capture of a row family may use.

    *fetch* makes one bounded GET and returns its body, status and resolved
    URL. *scan* hands a page to a consumer slice by slice under a byte ceiling
    and keeps none of it.
    """

    config_path: Path
    source: dict[str, Any]
    reference_date: date
    fetch: Callable[[str], dict[str, Any]]
    scan: Callable[[str, Callable[[str], None], int], dict[str, Any]]


@dataclass(frozen=True)
class RowFamily:
    """One row family's part in agent extraction.

    *lead_rules* replaces the shared opening rules of the contract by their
    position. *omitted_fields* are removed from the mention shape before
    *shape* is applied. *missing_status_error* is empty for a family that
    makes no request during a run.
    """

    error: type[ValueError]
    capture: Callable[[RowCapture], tuple[dict[str, Any], dict[str, Any]]]
    model_text: Callable[[dict[str, Any], dict[str, Any]], str]
    budget_error: str
    missing_status_error: str
    prompt_rows_label: str
    lead_rules: Mapping[int, str]
    rules: tuple[str, ...]
    omitted_fields: tuple[str, ...]
    shape: Callable[[Mapping[str, Any]], dict[str, Any]]
    required_fields: tuple[str, ...]
    capture_rules: Callable[[str], list[str]]


def _candidate_category(source: Mapping[str, Any]) -> Any:
    return source.get("eligible_candidate_category", "adult nonfiction")


def _bookinfo_capture(capture: RowCapture) -> tuple[dict[str, Any], dict[str, Any]]:
    bundle = bookinfo.capture(capture.source, capture.reference_date, capture.fetch)
    return bookinfo.page_data(bundle, capture.source), {
        "live_fetch": True,
        "bookinfo_period_start": bundle["period_start"],
    }


def _ipc_top40_capture(capture: RowCapture) -> tuple[dict[str, Any], dict[str, Any]]:
    def scan_page(target: str, consume: Callable[[str], None]) -> dict[str, Any]:
        return capture.scan(target, consume, ipc_top40.PAGE_CEILING_BYTES)

    bundle = ipc_top40.capture(capture.source, capture.reference_date, capture.fetch, scan_page)
    return ipc_top40.page_data(bundle, capture.source), {
        "live_fetch": True,
        "ipc_top40_published_date": bundle["published_date"],
    }


def _perlentaucher_capture(capture: RowCapture) -> tuple[dict[str, Any], dict[str, Any]]:
    # The index is only read here. It is written after extraction succeeds, so a capture
    # that never becomes mentions, such as a source health check, marks nothing as seen.
    seen = perlentaucher.read_seen_items(perlentaucher.seen_items_path(capture.config_path, capture.source))
    bundle = perlentaucher.capture(capture.source, capture.reference_date, capture.fetch, seen)
    return perlentaucher.page_data(bundle, capture.source), {
        "live_fetch": True,
        "perlentaucher_daily_pages": bundle["index"]["daily_pages"],
    }


def _kirkus_capture(capture: RowCapture) -> tuple[dict[str, Any], dict[str, Any]]:
    config_path, source = capture.config_path, capture.source
    # The site asks for twenty seconds between requests, so a run requests nothing: it reads the
    # result of the last pre-collection pass. A missing or old result is a named refusal.
    collection = kirkus.read_collection(kirkus.collection_path(config_path, source))
    seen = kirkus.read_seen_items(kirkus.seen_items_path(config_path, source))
    bundle = kirkus.capture(source, capture.reference_date, collection, seen)
    page = kirkus.page_data(bundle, source)
    return page, {
        "live_fetch": False,
        "kirkus_collected_date": bundle["collection"]["collected_date"],
        "kirkus_stop_reason": bundle["collection"]["stop_reason"],
        "kirkus_rows_waiting": kirkus.waiting_row_count(bundle["collection"]),
        # A pass the site cut short still reports the rows it reached, as a named incomplete source.
        # So does a run whose own pass failed and that reads the result of an earlier one, and a
        # run that leaves a listed book out for a reason other than a paid review (#873).
        **kirkus.coverage_debug(
            page,
            source,
            kirkus.failed_refresh_debug(kirkus.read_attempt(kirkus.attempt_path(config_path, source)), bundle),
        ),
    }


BOOKINFO = RowFamily(
    error=bookinfo.BookInfoError,
    capture=_bookinfo_capture,
    model_text=lambda page_data, source: json.dumps(
        bookinfo.validate_bundle(page_data.get("bookinfo_evidence", {}), source), ensure_ascii=False
    ),
    budget_error="BookInfo JSON rows exceed model input budget",
    missing_status_error="BookInfo response lacks an HTTP status",
    prompt_rows_label="ranking rows",
    lead_rules={
        0: "Use only the validated BookInfo JSON rows and obey source_metadata constraints.",
        1: "Read text for model input or bookinfo_evidence for a retained capture; never browse.",
        4: "Evidence URL must exactly match the observed ranking request URL retained with these rows.",
    },
    rules=(
        "Use the validated JSON rows in page_data.text, not HTML. Preserve exact title and supplier author display.",
        "Include isbn, integer current_rank, source_week, and candidate_category for each qualifying row.",
        "Use the canonical source_page as source_url and the observed ranking request URL as evidence_url.",
        "Keep original positions after exclusions; exclude fiction, children, activity books and keepsakes.",
        "Use signal_type bestseller/ranking and a metadata-only evidence note; do not copy publisher descriptions.",
        "Omit publication_year: the JSON states none. A publisher, when given, must be the row's publisher exactly.",
    ),
    omitted_fields=("publication_year",),
    shape=lambda source: {
        "isbn": "Captured ISBN-13",
        "current_rank": "Integer original rank 1..20",
        "source_week": "Captured selected period start YYYY-MM-DD",
        "candidate_category": _candidate_category(source),
        "publisher": "Captured row publisher, exactly",
    },
    required_fields=("isbn", "current_rank", "source_week", "candidate_category"),
    capture_rules=lambda capture_file: [
        f"Extract only from the saved capture file {capture_file}; its text field holds the validated "
        "JSON rows and its bookinfo_evidence field holds the retained responses. Its html field is empty.",
        "Before returning a mention, confirm its evidence_url equals the evidence_url of its row in the "
        f"text field of {capture_file}, which is the observed ranking request URL; never browse or follow "
        "links.",
    ],
)

IPC_TOP40 = RowFamily(
    error=ipc_top40.IpcTop40Error,
    capture=_ipc_top40_capture,
    model_text=lambda page_data, source: json.dumps(
        ipc_top40.validate_bundle(page_data.get("ipc_top40_evidence", {}), source), ensure_ascii=False
    ),
    budget_error="Independent Press Top 40 CSV rows exceed model input budget",
    missing_status_error="Independent Press Top 40 response lacks an HTTP status",
    prompt_rows_label="ranking rows",
    lead_rules={
        0: "Use only the validated Independent Press Top 40 CSV rows and obey source_metadata constraints.",
        1: "Read text for model input or ipc_top40_evidence for a retained capture; never browse.",
        4: "Evidence URL must exactly match the observed CSV file URL retained with these rows.",
    },
    rules=(
        "Use the validated CSV rows in page_data.text, not HTML. Preserve exact title.",
        "Copy each row's primary_authors array exactly; never use the joined author display as one author.",
        "Include isbn, integer current_rank, source_week, and candidate_category for each qualifying row.",
        "Use the canonical source_page as source_url and the observed CSV file URL as evidence_url.",
        "Keep original positions after exclusions; exclude fiction, children, activity books and keepsakes.",
        "Use signal_type bestseller/ranking and a metadata-only evidence note; do not copy publisher descriptions.",
        "Omit publication_year: the CSV states none. A publisher, when given, must be the CSV publisher exactly.",
    ),
    omitted_fields=("publication_year",),
    shape=lambda source: {
        "isbn": "Captured ISBN-13",
        "current_rank": "Integer original rank 1..40",
        "source_week": "Captured file publication date YYYY-MM-DD",
        "candidate_category": _candidate_category(source),
        "publisher": "Captured CSV publisher, exactly",
    },
    required_fields=("isbn", "current_rank", "source_week", "candidate_category"),
    capture_rules=lambda capture_file: [
        f"Extract only from the saved capture file {capture_file}; its text field holds the validated "
        "CSV rows and its ipc_top40_evidence field holds the retained file. Its html field is empty.",
        "Before returning a mention, confirm its evidence_url equals the evidence_url of its row in the "
        f"text field of {capture_file}, which is the observed CSV file URL; never browse or follow links.",
    ],
)

KIRKUS = RowFamily(
    error=kirkus.KirkusError,
    capture=_kirkus_capture,
    model_text=lambda page_data, source: kirkus.model_text(page_data.get(kirkus.EVIDENCE_FIELD, {}), source),
    budget_error="Kirkus listing rows exceed model input budget",
    missing_status_error="",
    prompt_rows_label="listing rows",
    lead_rules={
        0: "Use only the validated Kirkus listing rows and obey source_metadata constraints.",
        1: "Read the validated rows in the text field; never browse.",
        4: "Evidence URL must exactly match the review link retained with its row.",
    },
    rules=(
        "Use the validated rows in page_data.text, not HTML. Every row is a nonfiction book by the "
        "site's own genre label: return every row.",
        "Copy title, primary_authors, publisher, publication_year, isbn, source_week and evidence_url "
        "exactly from the row; never correct, complete, translate, reorder or merge them.",
        "The source prints every title in capitals. Return the title exactly as the row gives it; "
        "never change its capitalisation.",
        "Use the canonical source_page as source_url and the row's review link as evidence_url.",
        "Use signal_type media/curated list and a metadata-only evidence note; the rows carry no "
        "description, so return no content_description.",
    ),
    omitted_fields=("content_description", "autobiographical_subject_context"),
    shape=lambda source: {
        "isbn": "Captured ISBN-13",
        "source_week": "Captured review date YYYY-MM-DD",
        "candidate_category": _candidate_category(source),
        "publisher": "Captured publisher, exactly",
        "publication_year": "Captured integer publication year",
        "signal_strength": kirkus.signal_strength(source),
        "source_url": "Canonical source_page",
        "evidence_url": "Captured review link of the row",
    },
    required_fields=("isbn", "source_week", "candidate_category", "publisher", "publication_year"),
    capture_rules=lambda capture_file: [
        f"Extract only from the saved capture file {capture_file}; its text field is the complete list "
        "of rows to return, one row per book. Its kirkus_evidence field is the listing evidence "
        "behind those rows: never list, count or copy rows from it. Its html field is empty.",
        "Return exactly one mention per isbn. Before returning it, confirm that its source_week and "
        f"evidence_url equal those of its row in the text field of {capture_file}; never browse or "
        "follow links.",
    ],
)

PERLENTAUCHER = RowFamily(
    error=perlentaucher.PerlentaucherError,
    capture=_perlentaucher_capture,
    model_text=lambda page_data, source: perlentaucher.model_text(
        page_data.get(perlentaucher.EVIDENCE_FIELD, {}), source
    ),
    budget_error="Perlentaucher digest rows exceed model input budget",
    missing_status_error="Perlentaucher response lacks an HTTP status",
    prompt_rows_label="daily digest rows",
    lead_rules={
        0: "Use only the validated Perlentaucher daily digest rows and obey source_metadata constraints.",
        1: "Read the validated rows in the text field; never browse.",
        # The rows carry no genre statement, so the one decision asked of the extractor rests on what it
        # knows about the book. That knowledge may decide whether a row is returned and nothing else.
        2: (
            "Do not browse, search the web, or follow links yourself. Outside knowledge may be used for one "
            "decision only: whether a row is adult nonfiction. Never use it to change, complete or add a field."
        ),
        4: "Evidence URL must exactly match the evidence_url of its row.",
    },
    rules=(
        "Use the validated rows in page_data.text, not HTML. Decide for each row only whether the book "
        "is adult nonfiction, from its title, subtitle, authors and publisher.",
        "A row that is not adult nonfiction breaks a rule: leave out fiction, poetry, drama, comics and "
        "graphic novels, and children's and young-adult books. Return every other row.",
        "Copy title, primary_authors, publisher, publication_year, isbn, source_week and evidence_url "
        "exactly from the row; never correct, complete, translate, reorder or merge them.",
        "The subtitle is evidence for the decision only: do not return it and do not append it to the title.",
        "Use the canonical source_page as source_url and the row's book link as evidence_url.",
        "Use signal_type media/curated list and a metadata-only evidence note; the rows carry no "
        "description, so return no content_description.",
    ),
    omitted_fields=("content_description", "autobiographical_subject_context"),
    shape=lambda source: {
        "isbn": "Captured ISBN-13",
        "source_week": "Captured daily page date YYYY-MM-DD",
        "candidate_category": _candidate_category(source),
        "publisher": "Captured publisher, exactly",
        "publication_year": "Captured integer publication year",
        "signal_strength": perlentaucher.signal_strength(source),
        "source_url": "Canonical source_page",
        "evidence_url": "Captured book link of the row",
    },
    required_fields=("isbn", "source_week", "candidate_category", "publisher", "publication_year"),
    capture_rules=lambda capture_file: [
        f"Extract only from the saved capture file {capture_file}; its text field is the complete list "
        "of rows to decide on, one row per book. Its perlentaucher_evidence field is the per-day evidence "
        "behind those rows: never list, count or copy rows from it. Its html field is empty.",
        "Return at most one mention per isbn. Before returning it, confirm that its source_week and "
        f"evidence_url equal those of its row in the text field of {capture_file}; never browse or "
        "follow links.",
    ],
)

ROW_FAMILIES: dict[str, RowFamily] = {
    "bookinfo-weekly-json": BOOKINFO,
    "ipc-top40-csv": IPC_TOP40,
    "kirkus-coming-soon": KIRKUS,
    "perlentaucher-daily-digest": PERLENTAUCHER,
}


def row_family(source: Mapping[str, Any]) -> RowFamily | None:
    """The row family that governs *source*, or None for a source read from page HTML."""
    return ROW_FAMILIES.get(contract_family(dict(source)))
