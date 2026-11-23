from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from calendar import month_name
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.request import Request

from autoscout.helper_metadata import HelperMetadataError
from autoscout.helper_metadata import enrich_with_helper_metadata
from autoscout.helper_metadata import load_helper_metadata
from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.source_collectors.base import CollectorResult
from autoscout.source_collectors.base import _collector_error
from autoscout.source_collectors.base import clean_text
from autoscout.source_collectors.base import collector_request_headers
from autoscout.source_collectors.base import collectors_pkg
from autoscout.source_collectors.base import likely_blocked_response
from autoscout.source_collectors.base import resolve_config_path
from autoscout.source_collectors.base import same_domain
from autoscout.source_proposals import normalize_source_identity


@dataclass(frozen=True)
class NonfictionFrCollector:
    """Metadata-only adapter for the official Nonfiction.fr reviews RSS.

    The feed is only a discovery index. Book identity is accepted solely
    from the bounded book card on a same-domain review page; feed
    descriptions and article bodies are never returned or persisted.
    """

    variant: str

    def collect(
        self,
        config_path: Path,
        output_dir: Path,
        source: dict[str, Any],
        run_date: str,
    ) -> CollectorResult:
        live_fetch = self.variant == "live" and not (
            source.get("rss_fixture_file") or source.get("review_pages_fixture_file")
        )
        if live_fetch:
            rss_text, debug_metadata = live_nonfiction_fr_rss(source)
            pages: dict[str, Any] = {}
        else:
            rss_text, pages, debug_metadata = configured_nonfiction_fr_inputs(config_path, source)
            if self.variant == "live":
                debug_metadata["live_fixture_replay"] = True
        entries = nonfiction_fr_rss_entries(rss_text)
        unique_entries: list[dict[str, str]] = []
        feed_guids: set[str] = set()
        for entry in entries:
            guid = str(entry.get("guid", "")).strip()
            if guid and guid in feed_guids:
                continue
            if guid:
                feed_guids.add(guid)
            unique_entries.append(entry)
        seen_guids = nonfiction_fr_seen_guids(config_path, source)
        new_entries = [entry for entry in unique_entries if str(entry.get("guid", "")).strip() not in seen_guids]
        max_new_items = nonfiction_fr_max_new_items(source)
        deferred_guid_count = max(0, len(new_entries) - max_new_items)
        new_entries = new_entries[:max_new_items]
        mentions: list[dict[str, Any]] = []
        dropped: list[dict[str, str]] = []
        processed_guids: list[str] = []
        raw_mention_count = len(new_entries)

        for entry in new_entries:
            guid = str(entry.get("guid", "")).strip()
            item_url = str(entry.get("link", "")).strip()
            if not guid or not item_url or not same_domain(str(source.get("url", "")), item_url):
                dropped.append({"guid": guid, "reason": "invalid_or_cross_domain_item"})
                continue
            page = pages.get(item_url)
            if live_fetch:
                page = live_nonfiction_fr_review_page(item_url, source)
            if not isinstance(page, dict):
                dropped.append({"guid": guid, "reason": "review_page_unavailable"})
                continue
            resolved_url = str(page.get("resolved_url") or item_url).strip()
            if normalize_source_identity(resolved_url) != normalize_source_identity(item_url):
                dropped.append({"guid": guid, "reason": "wrong_review_target"})
                continue
            blocks = nonfiction_fr_book_blocks(str(page.get("html", "")))
            # A joint card holds one raw book per block; each feed entry
            # already counts for one, so count the extra blocks found.
            raw_mention_count += max(0, len(blocks) - 1)
            review_date = nonfiction_fr_review_date(str(entry.get("pub_date", "")))
            complete_blocks = [block for block in blocks if _is_complete_book(block)]
            if not review_date or not complete_blocks:
                dropped.extend(
                    {"guid": guid, "reason": "not_a_complete_book_review"} for _ in range(max(1, len(blocks)))
                )
                processed_guids.append(guid)
                continue
            # Failing closed is per book: an incomplete block in a multi-book
            # card is recorded and dropped without losing its complete siblings.
            for block in blocks:
                if not _is_complete_book(block):
                    dropped.append({"guid": guid, "reason": "incomplete_book_in_card"})
            for metadata in complete_blocks:
                mentions.append(
                    {
                        "title": metadata["title"],
                        "primary_authors": metadata["primary_authors"],
                        "publisher": metadata["publisher"],
                        "publication_year": metadata["publication_year"],
                        "source_url": item_url,
                        "evidence_url": item_url,
                        "source_check_date": run_date,
                        "signal_type": "media/curated list",
                        "signal_strength": "minor mention",
                        "evidence_note": f"Reviewed by Nonfiction.fr on {review_date}; metadata-only evidence.",
                        "candidate_category": "adult nonfiction",
                    }
                )
            processed_guids.append(guid)

        # The RSS collector has no extraction model, so it cannot translate.
        # A curated helper sidecar (same version-1 shape as the Codex path)
        # supplies the non-authoritative English helpers for French mentions;
        # without one the mentions flow without helpers (Issue #645).
        helper_attached_count = 0
        if source.get("helper_metadata_file"):
            try:
                loaded_index = load_helper_metadata(config_path, source)
            except HelperMetadataError as exc:
                raise _collector_error(
                    "Nonfiction.fr helper metadata failed closed",
                    raw_mention_count=raw_mention_count,
                    valid_mention_count=0,
                    skipped_item_count=len(dropped),
                    debug_metadata={
                        "collector_kind": "nonfiction-fr",
                        "safe_failure_status": exc.reason_code,
                    },
                ) from exc
            if loaded_index is None:  # Unreachable: a configured file always loads or raises above.
                raise _collector_error(
                    "Nonfiction.fr helper metadata failed closed",
                    raw_mention_count=raw_mention_count,
                    valid_mention_count=0,
                    skipped_item_count=len(dropped),
                    debug_metadata={
                        "collector_kind": "nonfiction-fr",
                        "safe_failure_status": "helper_metadata_missing",
                    },
                )
            helper_index = loaded_index
            enriched_mentions: list[dict[str, Any]] = []
            for mention in mentions:
                enriched, identity = enrich_with_helper_metadata(mention, helper_index)
                if identity is not None:
                    helper_attached_count += 1
                enriched_mentions.append(enriched)
            mentions = enriched_mentions

        if self.variant == "live":
            persist_nonfiction_fr_seen_guids(config_path, source, seen_guids, processed_guids, run_date)

        debug_metadata.update(
            {
                "collector_kind": "nonfiction-fr",
                "feed_item_count": len(entries),
                "duplicate_guid_count": len(entries) - len(unique_entries),
                "seen_guid_count": len(seen_guids),
                "new_guid_count": len(new_entries),
                "deferred_guid_count": deferred_guid_count,
                "processed_guid_count": len(processed_guids),
                "validated_mention_count": len(mentions),
                "dropped_mention_count": len(dropped),
                "drop_reasons": dropped,
                "helper_attached_count": helper_attached_count,
            }
        )
        return CollectorResult(
            mentions=mentions,
            raw_mention_count=raw_mention_count,
            valid_mention_count=len(mentions),
            skipped_item_count=len(dropped),
            debug_metadata=debug_metadata,
        )


def configured_nonfiction_fr_inputs(
    config_path: Path, source: dict[str, Any]
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    raw_rss_path = str(source.get("rss_fixture_file", "")).strip()
    raw_pages_path = str(source.get("review_pages_fixture_file", "")).strip()
    if not raw_rss_path or not raw_pages_path:
        raise _collector_error(
            "Nonfiction.fr replay requires RSS and review-page fixtures",
            debug_metadata={"collector_kind": "nonfiction-fr", "safe_failure_status": "missing_fixture"},
        )
    rss_path = resolve_config_path(config_path, raw_rss_path)
    pages_path = resolve_config_path(config_path, raw_pages_path)
    try:
        rss_text = rss_path.read_text(encoding="utf-8")
        pages_data = json.loads(pages_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise _collector_error(
            f"Nonfiction.fr fixture not found: {exc.filename}",
            debug_metadata={"collector_kind": "nonfiction-fr", "safe_failure_status": "missing_fixture"},
        )
    except json.JSONDecodeError:
        raise _collector_error(
            "Nonfiction.fr review-page fixture is malformed JSON",
            debug_metadata={"collector_kind": "nonfiction-fr", "safe_failure_status": "malformed_fixture"},
        )
    pages = pages_data.get("pages", {}) if isinstance(pages_data, dict) else {}
    if not isinstance(pages, dict):
        raise _collector_error(
            "Nonfiction.fr review-page fixture pages must be an object",
            debug_metadata={"collector_kind": "nonfiction-fr", "safe_failure_status": "malformed_fixture"},
        )
    return (
        rss_text,
        pages,
        {
            "rss_fixture_file": raw_rss_path,
            "review_pages_fixture_file": raw_pages_path,
            "live_fetch": False,
        },
    )


def nonfiction_fr_max_new_items(source: dict[str, Any]) -> int:
    raw_value = source.get("max_new_items_per_run", 12)
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        raise _collector_error(
            "Nonfiction.fr max_new_items_per_run must be an integer",
            debug_metadata={"collector_kind": "nonfiction-fr", "safe_failure_status": "invalid_item_limit"},
        )
    if value <= 0:
        raise _collector_error(
            "Nonfiction.fr max_new_items_per_run must be positive",
            debug_metadata={"collector_kind": "nonfiction-fr", "safe_failure_status": "invalid_item_limit"},
        )
    return value


def _fetch_nonfiction_fr_document(url: str, source: dict[str, Any], *, max_bytes: int) -> tuple[str, str]:
    request = Request(url, headers=collector_request_headers())
    try:
        timeout = float(source.get("timeout_seconds", 10))
    except (TypeError, ValueError):
        raise _collector_error(
            "Nonfiction.fr timeout_seconds must be a number",
            debug_metadata={"collector_kind": "nonfiction-fr", "safe_failure_status": "invalid_timeout"},
        )
    if timeout <= 0:
        raise _collector_error(
            "Nonfiction.fr timeout_seconds must be positive",
            debug_metadata={"collector_kind": "nonfiction-fr", "safe_failure_status": "invalid_timeout"},
        )
    try:
        with collectors_pkg().urlopen(request, timeout=timeout) as response:
            resolved_url = response.geturl()
            body = response.read(max_bytes + 1)
    except TimeoutError:
        raise _collector_error(
            "Nonfiction.fr live collection failed: request timed out",
            debug_metadata={"collector_kind": "nonfiction-fr", "safe_failure_status": "timeout"},
        )
    except HTTPError as exc:
        status = "skipped" if exc.code in {401, 403, 429} else "failed"
        raise _collector_error(
            f"Nonfiction.fr live collection failed: HTTP {exc.code}",
            status=status,
            debug_metadata={
                "collector_kind": "nonfiction-fr",
                "safe_failure_status": "source_blocked" if status == "skipped" else "http_error",
                "http_status": str(exc.code),
            },
        )
    except URLError as exc:
        raise _collector_error(
            f"Nonfiction.fr live collection failed: {exc.reason}",
            debug_metadata={"collector_kind": "nonfiction-fr", "safe_failure_status": "network_error"},
        )
    if len(body) > max_bytes:
        raise _collector_error(
            "Nonfiction.fr live collection failed: response exceeded the metadata capture limit",
            debug_metadata={"collector_kind": "nonfiction-fr", "safe_failure_status": "response_too_large"},
        )
    text = body.decode("utf-8", errors="replace")
    if likely_blocked_response(text):
        raise _collector_error(
            "Nonfiction.fr live collection blocked by CAPTCHA-like response",
            status="skipped",
            debug_metadata={"collector_kind": "nonfiction-fr", "safe_failure_status": "source_blocked"},
        )
    return resolved_url, text


def live_nonfiction_fr_rss(source: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if not source.get("enable_live"):
        raise _collector_error(
            "Nonfiction.fr live collection requires enable_live: true",
            status="skipped",
            debug_metadata={"collector_kind": "nonfiction-fr", "safe_failure_status": "disabled"},
        )
    url = str(source.get("url", "")).strip()
    if not url:
        raise _collector_error("Nonfiction.fr live collection requires a configured RSS URL")
    resolved_url, rss_text = _fetch_nonfiction_fr_document(url, source, max_bytes=400_000)
    if normalize_source_identity(resolved_url) != normalize_source_identity(url):
        raise _collector_error(
            "Nonfiction.fr RSS resolved to a different source target",
            debug_metadata={
                "collector_kind": "nonfiction-fr",
                "safe_failure_status": "wrong_target",
                "resolved_url": resolved_url,
            },
        )
    return rss_text, {"live_fetch": True, "source_url": url, "resolved_url": resolved_url}


def live_nonfiction_fr_review_page(item_url: str, source: dict[str, Any]) -> dict[str, str]:
    resolved_url, markup = _fetch_nonfiction_fr_document(item_url, source, max_bytes=500_000)
    return {"resolved_url": resolved_url, "html": markup}


def nonfiction_fr_seen_guids(config_path: Path, source: dict[str, Any]) -> dict[str, str]:
    raw_path = str(source.get("seen_guids_fixture_file") or source.get("seen_guids_file") or "").strip()
    if not raw_path:
        return {}
    path = resolve_config_path(config_path, raw_path)
    try:
        data = read_json(path, default={})
    except json.JSONDecodeError:
        raise _collector_error(
            "Nonfiction.fr seen-GUID state is malformed JSON",
            debug_metadata={"collector_kind": "nonfiction-fr", "safe_failure_status": "malformed_seen_state"},
        )
    if not isinstance(data, dict) or not isinstance(data.get("seen_guids", []), list):
        raise _collector_error(
            "Nonfiction.fr seen-GUID state has an invalid shape",
            debug_metadata={"collector_kind": "nonfiction-fr", "safe_failure_status": "malformed_seen_state"},
        )
    seen: dict[str, str] = {}
    for entry in data.get("seen_guids", []):
        if not isinstance(entry, dict):
            continue
        guid = str(entry.get("guid", "")).strip()
        if guid:
            seen[guid] = str(entry.get("first_seen_date", "")).strip()
    return seen


def persist_nonfiction_fr_seen_guids(
    config_path: Path,
    source: dict[str, Any],
    seen_guids: dict[str, str],
    processed_guids: list[str],
    run_date: str,
) -> None:
    raw_path = str(source.get("seen_guids_file", "")).strip()
    if not raw_path or not processed_guids:
        return
    merged = dict(seen_guids)
    for guid in processed_guids:
        merged.setdefault(guid, run_date)
    path = resolve_config_path(config_path, raw_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(
        path,
        {
            "source": str(source.get("name", "Nonfiction.fr Critiques")),
            "updated_at": run_date,
            "seen_guids": [
                {"guid": guid, "first_seen_date": first_seen_date} for guid, first_seen_date in merged.items()
            ],
        },
    )


def _nonfiction_fr_rss_item_text(item: ET.Element, field: str) -> str:
    return next(
        ((child.text or "").strip() for child in item if child.tag.rsplit("}", 1)[-1] == field),
        "",
    )


def nonfiction_fr_rss_entries(rss_text: str) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(rss_text)
    except ET.ParseError as exc:
        raise _collector_error(
            f"Nonfiction.fr RSS is malformed XML: {exc}",
            debug_metadata={"collector_kind": "nonfiction-fr", "safe_failure_status": "malformed_rss"},
        )
    entries: list[dict[str, str]] = []
    for item in root.findall("./channel/item"):
        entries.append(
            {
                "guid": _nonfiction_fr_rss_item_text(item, "guid"),
                "link": _nonfiction_fr_rss_item_text(item, "link"),
                "pub_date": _nonfiction_fr_rss_item_text(item, "pubDate"),
            }
        )
    return entries


def nonfiction_fr_book_blocks(markup: str) -> list[dict[str, Any]]:
    """Return one metadata dict per book block in the review's book card.

    A joint review card holds one container per reviewed book. When those
    containers are present, each starts a book: a container with exactly one ``<h6>``
    title is that book, and a container with no title or several titles
    cannot be attributed to one book, so it yields incomplete blocks rather
    than lending its authors or facts to a neighbour. A card without identified
    book containers falls back to the titles: everything from one ``<h6>``
    up to the next (or the end of the card) belongs to that book, and content
    before the first ``<h6>`` belongs to no book. Fields a block does not carry stay
    empty so the caller can fail closed per book instead of per article.
    """
    book_card = re.search(
        r"<aside\b[^>]*class=[\"'][^\"']*\bpost-content__book\b[^\"']*[\"'][^>]*>(.*?)</aside>",
        markup,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not book_card:
        return []
    card = book_card.group(1)
    containers = _nonfiction_fr_book_containers(card)
    if not containers:
        headings = list(_NONFICTION_FR_TITLE.finditer(card))
        return [
            _nonfiction_fr_book_block(
                clean_text(heading.group(1)),
                card[heading.end() : headings[index + 1].start() if index + 1 < len(headings) else len(card)],
            )
            for index, heading in enumerate(headings)
        ]
    blocks: list[dict[str, Any]] = []
    for segment in containers:
        titles = list(_NONFICTION_FR_TITLE.finditer(segment))
        if len(titles) == 1:
            blocks.append(_nonfiction_fr_book_block(clean_text(titles[0].group(1)), segment))
        elif len(titles) > 1:
            blocks.extend(_nonfiction_fr_book_block(clean_text(title.group(1)), "") for title in titles)
        else:
            blocks.append(_nonfiction_fr_book_block("", segment))
    return blocks


_NONFICTION_FR_TITLE = re.compile(r"<h6\b[^>]*>(.*?)</h6>", flags=re.IGNORECASE | re.DOTALL)
_NONFICTION_FR_DIV_TAG = re.compile(r"</?div\b[^>]*>", flags=re.IGNORECASE)


def _nonfiction_fr_book_containers(card: str) -> list[str]:
    """Extract the site's book-card divs without treating inner images as books."""
    stack: list[tuple[int, bool]] = []
    found: list[tuple[int, int]] = []
    for match in _NONFICTION_FR_DIV_TAG.finditer(card):
        if match.group().startswith("</"):
            if not stack:
                continue
            start, is_book = stack.pop()
            if is_book:
                found.append((start, match.end()))
            continue
        class_match = re.search(r"\sclass\s*=\s*[\"']([^\"']*)[\"']", match.group(), flags=re.IGNORECASE)
        classes = set(class_match.group(1).split()) if class_match else set()
        stack.append((match.start(), "flex" in classes))
    found.sort()
    selected: list[tuple[int, int]] = []
    for start, end in found:
        if not selected or start >= selected[-1][1]:
            selected.append((start, end))
    return [card[start:end] for start, end in selected]


def _nonfiction_fr_book_block(title: str, segment: str) -> dict[str, Any]:
    authors = [
        clean_text(value)
        for value in re.findall(
            r"<a\b[^>]*href=[\"'][^\"']*fiche-auteur[^\"']*[\"'][^>]*>(.*?)</a>",
            segment,
            flags=re.IGNORECASE | re.DOTALL,
        )
    ]
    facts = [
        clean_text(value)
        for value in re.findall(
            r"<p\b[^>]*class=[\"'][^\"']*\bf-pn\b[^\"']*[\"'][^>]*>(.*?)</p>",
            segment,
            flags=re.IGNORECASE | re.DOTALL,
        )
    ]
    years = [fact for fact in facts if re.fullmatch(r"(?:19|20)\d{2}", fact)]
    page_counts = [fact for fact in facts if re.fullmatch(r"\d+\s+pages?", fact, flags=re.IGNORECASE)]
    if len(years) > 1 or len(page_counts) > 1:
        # A book has one year and one page count, so a repeat means the
        # block holds more than one book's facts: fail closed.
        facts = []
    publication_year = next((fact for fact in facts if re.fullmatch(r"(?:19|20)\d{2}", fact)), "")
    publisher = next(
        (
            fact
            for fact in facts
            if fact != publication_year and not re.fullmatch(r"\d+\s+pages?", fact, flags=re.IGNORECASE)
        ),
        "",
    )
    return {
        "title": title,
        "primary_authors": [author for author in authors if author],
        "publication_year": publication_year,
        "publisher": publisher,
    }


def _is_complete_book(block: dict[str, Any]) -> bool:
    """Say whether a book block carries the full Source Mention identity."""
    return bool(block["title"] and block["primary_authors"] and block["publication_year"] and block["publisher"])


def nonfiction_fr_review_date(raw_date: str) -> str:
    try:
        value = parsedate_to_datetime(raw_date)
    except (TypeError, ValueError, OverflowError):
        return ""
    return f"{value.day} {month_name[value.month]} {value.year}"
