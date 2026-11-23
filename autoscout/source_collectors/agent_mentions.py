"""Turn an extraction response into validated mentions and dropped rows."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

from autoscout.author_admission import NO_PERSON_AUTHOR_DROP_REASON
from autoscout.author_admission import PLACEHOLDER_AUTHOR_DROP_REASON
from autoscout.author_admission import admitted_author_people
from autoscout.author_admission import is_placeholder_author
from autoscout.author_admission import without_placeholder_authors
from autoscout.author_grounding import AUTHOR_NOT_IN_CAPTURE_DROP_REASON
from autoscout.author_grounding import CaptureAuthorText
from autoscout.autobiographical_context import ground_autobiographical_subject_context
from autoscout.helper_metadata import HelperMetadataError
from autoscout.helper_metadata import HelperMetadataIndex
from autoscout.helper_metadata import drop_title_duplicate_helpers
from autoscout.helper_metadata import enrich_with_helper_metadata
from autoscout.identity import normalize_isbn
from autoscout.isbn_author_verification import verified_author
from autoscout.partial_list import keeps_partial_list
from autoscout.ranking_author_policy import mention_author_exempt
from autoscout.ranking_detail_route import RankingDetailRoute
from autoscout.source_collectors.base import FORBIDDEN_COLLECTOR_RESPONSE_FIELDS
from autoscout.source_collectors.base import CollectorRunError
from autoscout.source_collectors.base import _collector_error
from autoscout.source_collectors.base import contract_family
from autoscout.source_collectors.base import evidence_url_verified
from autoscout.source_collectors.base import extraction_contract_violation
from autoscout.source_collectors.base import extraction_entries
from autoscout.source_collectors.base import normalize_chart_evidence_url
from autoscout.source_collectors.base import normalized_agent_mention
from autoscout.source_collectors.base import page_links
from autoscout.source_collectors.base import same_domain
from autoscout.source_collectors.base import with_observed_ranking_week
from autoscout.source_page_identity import ParsedCapture
from autoscout.source_page_identity import amazon_charts_entries
from autoscout.source_page_identity import audible_chart_entries
from autoscout.source_page_identity import audible_chart_entry_title
from autoscout.source_page_identity import audible_chart_ranks
from autoscout.source_page_identity import ranking_entry_isbn


def with_audible_chart_title(
    entry: dict[str, Any], source: dict[str, Any], page_data: dict[str, Any], *, parsed: ParsedCapture | None = None
) -> dict[str, Any]:
    """Store the chart's own title when an Audible title was joined with its subtitle (Issue #835).

    Only the English Audible chart path is affected; the grounding check that
    follows is unchanged, so any other title is still grounded or refused.
    """
    if source.get("audible_explicit_chart") is not True or str(source.get("language", "")).strip().casefold() == "de":
        return entry
    title = str(entry.get("title", ""))
    evidence_url = str(entry.get("evidence_url") or entry.get("item_url") or "")
    if not title or not evidence_url:
        return entry
    chart_title = audible_chart_entry_title(
        str(page_data.get("html", "")),
        str(page_data.get("source_page", "")),
        title=title,
        evidence_url=normalize_chart_evidence_url(evidence_url, source),
        parsed=parsed,
    )
    if chart_title == title:
        return entry
    return {**entry, "title": chart_title}


def claimed_rank(entry: Any) -> int | None:
    """The chart rank a row claims, or None when it states no usable rank."""
    if not isinstance(entry, dict):
        return None
    try:
        return int(str(entry.get("current_rank", "")).strip())
    except ValueError:
        return None


def _chart_ranks_kept(
    ranks: list[int], expected_ranks: list[int], *, extracted: int, dropped_ranks: list[int | None]
) -> bool:
    """Whether a chart's kept rows give its exact ranks, or a partial list of them (Issue #839).

    No rank may be claimed twice by any rows, kept or dropped. A complete
    chart is then kept as before. A chart missing some ranks keeps its
    verified rows only when every kept rank is a chart rank, every missing rank
    was claimed by a dropped row, and at least 80% of the extracted rows
    passed. A rank the extraction never returned, or a rank given twice, still
    fails the chart.
    """
    claimed = [*ranks, *(rank for rank in dropped_ranks if rank is not None)]
    if len(set(claimed)) != len(claimed):
        return False
    if sorted(ranks) == expected_ranks:
        return True
    if not set(ranks) <= set(expected_ranks):
        return False
    if not set(expected_ranks) - set(ranks) <= set(dropped_ranks):
        return False
    return keeps_partial_list(len(ranks), extracted)


def _dropped_row(mention: dict[str, Any], index: int, evidence_url: str, reason: str) -> dict[str, str]:
    """The record kept for one extracted row that did not become a mention."""
    return {
        "title": str(mention.get("title") or f"mention {index}"),
        "evidence_url": evidence_url,
        "reason": reason,
    }


def agent_extraction_mentions_from_response(
    page_data: dict[str, Any],
    extraction_data: Any,
    source: dict[str, Any],
    run_date: str,
    *,
    collector_kind: str = "agent-extraction",
    helper_index: HelperMetadataIndex | None = None,
    upstream_dropped_ranks: Sequence[int | None] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Return the mentions that pass validation and the dropped rows.

    *upstream_dropped_ranks* holds the claimed rank of each extracted row
    dropped before this call (missing fields, or refused by the artifact
    gate; None when it stated none), so a chart's 80% rule counts every row
    the extraction returned (Issue #839).
    """
    if isinstance(extraction_data, dict) and extraction_data.get("status"):
        status = str(extraction_data.get("status", "")).strip().lower()
        if status in {"captcha", "blocked", "access-denied"}:
            raise _collector_error(
                "Agent extraction skipped: source or backend blocked",
                status="skipped",
                debug_metadata={
                    "safe_debug_status": status,
                    "safe_failure_status": status,
                    "collector_kind": collector_kind,
                },
            )
        # Explicit success statuses from the extraction backend are not errors.
        if status in {"ok", "success", "complete"}:
            pass
        else:
            raise _collector_error(
                "Agent extraction failed: response metadata could not be parsed safely",
                debug_metadata={
                    "safe_debug_status": status,
                    "safe_failure_status": status,
                    "collector_kind": collector_kind,
                },
            )
    code_normalized = isinstance(extraction_data, dict) and extraction_data.get("_code_normalized") is True
    entries = extraction_entries(extraction_data)
    if not isinstance(entries, list):
        raise _collector_error("Agent extraction response mentions must be a list")

    source_page = str(page_data.get("source_page", "")).strip()
    html_text = str(page_data.get("html", ""))
    verified_links = page_links(html_text, source_page) if html_text else None
    # Every mention's grounding check reads this one capture of the page, so
    # the page is parsed once per pass, not once per mention (Issue #718).
    parsed = ParsedCapture(html_text, source_page)
    capture_text: CaptureAuthorText | None = None

    mentions: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []
    contract_dropped: list[dict[str, str]] = []
    dropped_ranks: list[int | None] = list(upstream_dropped_ranks)
    detail_route = RankingDetailRoute.from_source(source)
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise _collector_error(f"Agent extraction mention {index} must be an object")
        forbidden = FORBIDDEN_COLLECTOR_RESPONSE_FIELDS & set(entry)
        if forbidden:
            raise _collector_error(
                f"Agent extraction response stores forbidden copied text fields: {sorted(forbidden)}",
                raw_mention_count=len(entries),
                valid_mention_count=len(mentions),
                skipped_item_count=len(dropped),
                debug_metadata={
                    "safe_failure_status": "forbidden_copied_text",
                    "forbidden_fields": sorted(forbidden),
                    "collector_kind": collector_kind,
                },
            )
        contract_entry = drop_title_duplicate_helpers(entry)
        if helper_index is not None:
            try:
                contract_entry, _helper_identity = enrich_with_helper_metadata(contract_entry, helper_index)
            except HelperMetadataError as exc:
                raise _collector_error(
                    "Agent extraction helper metadata failed closed",
                    raw_mention_count=len(entries),
                    valid_mention_count=len(mentions),
                    skipped_item_count=len(dropped),
                    debug_metadata={
                        "collector_kind": collector_kind,
                        "safe_failure_status": exc.reason_code,
                    },
                ) from exc
        contract_entry = with_observed_ranking_week(contract_entry, source, page_data)
        contract_entry = with_audible_chart_title(contract_entry, source, page_data, parsed=parsed)
        mention = normalized_agent_mention(contract_entry, source, run_date)
        if code_normalized:
            contract_entry.pop("source_check_date", None)
            contract_entry.pop("retailer_discovery", None)
        # An extractor's placeholder author ("not stated in HTML") is not a person
        # (Issue #852), and neither is a credential on its own such as "MD"
        # (Issue #854). Both are refused before any identity key exists; a row
        # left with no person follows the source's author-optional rule.
        authors = mention.get("primary_authors")
        if isinstance(authors, list) and any(str(author).strip() for author in authors):
            has_placeholder = any(is_placeholder_author(author) for author in authors)
            people = without_placeholder_authors(authors)
            names_somebody = any(admitted_author_people(author) for author in people)
            if not names_somebody and not mention_author_exempt(source):
                dropped.append(
                    _dropped_row(
                        mention,
                        index,
                        str(mention.get("evidence_url") or mention.get("source_url", "")).strip(),
                        PLACEHOLDER_AUTHOR_DROP_REASON if has_placeholder else NO_PERSON_AUTHOR_DROP_REASON,
                    )
                )
                dropped_ranks.append(claimed_rank(contract_entry))
                continue
            if has_placeholder:
                mention["primary_authors"] = people
                for field_name in ("primary_authors", "authors"):
                    if isinstance(contract_entry.get(field_name), list):
                        contract_entry[field_name] = without_placeholder_authors(contract_entry[field_name])
        contract_reason = extraction_contract_violation(
            contract_entry, mention, source, page_data, detail_route=detail_route, parsed=parsed
        )
        evidence_url = str(mention.get("evidence_url") or mention.get("source_url", "")).strip()
        if contract_reason:
            contract_drop = _dropped_row(mention, index, evidence_url, contract_reason)
            dropped.append(contract_drop)
            contract_dropped.append(contract_drop)
            dropped_ranks.append(claimed_rank(contract_entry))
            continue
        # The ranking check grounds the ISBN inside the value, so a labelled
        # ISBN is stored bare and the book keeps its identity (Issue #827).
        # A value the identity key already reads as that ISBN stays as given.
        if contract_family(source) == "ranking-table":
            raw_isbn = str(mention.get("isbn", ""))
            bare_isbn = ranking_entry_isbn(raw_isbn)
            if bare_isbn and normalize_isbn(raw_isbn).casefold() != bare_isbn.casefold():
                mention["isbn"] = bare_isbn
        url_verified, drop_reason = evidence_url_verified(page_data, evidence_url, links=verified_links)
        if not url_verified and source.get("amazon_charts_weekly") is True:
            evidence_match = re.match(r"^/dp/([A-Z0-9]{10})$", urlsplit(evidence_url).path)
            if evidence_match is not None:
                url_verified = any(
                    same_domain(source_page, href)
                    and (entry_match := re.match(r"^/dp/([A-Z0-9]{10})", urlsplit(href).path)) is not None
                    and entry_match.group(1) == evidence_match.group(1)
                    for href, _title, _context in amazon_charts_entries(html_text, source_page, parsed=parsed)
                )
                if url_verified:
                    drop_reason = ""
        if not url_verified:
            dropped_ranks.append(claimed_rank(contract_entry))
            dropped.append(_dropped_row(mention, index, evidence_url, drop_reason))
            continue
        # Every person named must be in the capture: an author the page never
        # names becomes an identity beside the real book (Issue #850). The
        # source contract and the evidence URL check name their drops first.
        people = without_placeholder_authors(mention.get("primary_authors") or [])
        if people:
            capture_text = capture_text or CaptureAuthorText(page_data)
            vouched: tuple[str, ...] = ()
            if contract_family(source) == "ranking-table":
                try:
                    vouched = (
                        verified_author(
                            source, ranking_entry_isbn(str(mention.get("isbn", ""))), str(mention.get("title", ""))
                        ),
                    )
                except ValueError:
                    vouched = ()
            if capture_text.missing_people(people, evidence_url, vouched_authors=vouched):
                dropped.append(_dropped_row(mention, index, evidence_url, AUTHOR_NOT_IN_CAPTURE_DROP_REASON))
                dropped_ranks.append(claimed_rank(contract_entry))
                continue
        if mention.get("autobiographical_subject_context"):
            grounded_context = ground_autobiographical_subject_context(
                mention["autobiographical_subject_context"],
                source_html=html_text,
                evidence_url=evidence_url,
                source_page=source_page,
            )
            if grounded_context:
                mention["autobiographical_subject_context"] = grounded_context
            else:
                mention.pop("autobiographical_subject_context", None)
        mentions.append(mention)
    audible_chart = source.get("audible_explicit_chart") is True
    if not audible_chart and source.get("amazon_charts_weekly") is not True:
        return mentions, dropped

    def contract_rejection(*, every_entry: bool) -> CollectorRunError:
        contract_rules = "; ".join(sorted({drop["reason"] for drop in contract_dropped}))
        contract_count = len(contract_dropped)
        entry_label = "entry" if contract_count == 1 else "entries"
        rejected = f"all {contract_count} entries" if every_entry else f"{contract_count} {entry_label}"
        chart_name = "Audible explicit chart" if audible_chart else "Amazon Charts weekly"
        return _collector_error(
            f"{chart_name} extraction contract rejected {rejected}: {contract_rules}",
            raw_mention_count=len(entries),
            valid_mention_count=len(mentions),
            skipped_item_count=len(dropped),
            debug_metadata={
                "safe_failure_status": "contract_violation",
                "collector_kind": collector_kind,
                "contract_rejected_count": contract_count,
                "contract_drop_reasons": contract_dropped,
                "drop_reasons": dropped,
            },
        )

    if not mentions and contract_dropped and len(contract_dropped) == len(entries):
        raise contract_rejection(every_entry=True)
    expected_ranks = list(range(1, 21))
    failure = (
        "Audible explicit chart failed: expected exact ranks 1 through 20"
        if audible_chart
        else "Amazon Charts explicit chart failed: expected exact ranks 1 through 20"
    )
    if audible_chart and str(source.get("language", "")).strip().casefold() == "de":
        # The German chart excludes rows whose title identifies a podcast
        # season from extraction while their chart positions remain
        # validated on the page (Issue #366): the expected rank set is
        # the page's ranks minus those excluded rows.
        expected_ranks = [
            rank
            for rank, (_href, title, _context) in zip(
                audible_chart_ranks(html_text, source_page, parsed=parsed),
                audible_chart_entries(html_text, source_page, parsed=parsed),
                strict=True,
            )
            if "Podcast" not in title
        ]
        failure = "Audible explicit chart failed: expected exact eligible chart ranks excluding podcast-season rows"
    if not _chart_ranks_kept(
        [int(str(mention["current_rank"])) for mention in mentions],
        expected_ranks,
        extracted=len(entries) + len(upstream_dropped_ranks),
        dropped_ranks=dropped_ranks,
    ):
        if contract_dropped:
            raise contract_rejection(every_entry=False)
        raise _collector_error(
            failure, debug_metadata={"safe_failure_status": "page_shape", "collector_kind": collector_kind}
        )
    return mentions, dropped
