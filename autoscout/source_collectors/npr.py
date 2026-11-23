from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.request import Request

from autoscout.source_collectors.base import FORBIDDEN_COLLECTOR_RESPONSE_FIELDS
from autoscout.source_collectors.base import CollectorResult
from autoscout.source_collectors.base import _collector_error
from autoscout.source_collectors.base import author_from_card
from autoscout.source_collectors.base import capture_live_response
from autoscout.source_collectors.base import capture_normalized_mentions
from autoscout.source_collectors.base import clean_text
from autoscout.source_collectors.base import collector_request_headers
from autoscout.source_collectors.base import collectors_pkg
from autoscout.source_collectors.base import likely_blocked_response
from autoscout.source_collectors.base import resolve_config_path
from autoscout.source_collectors.base import source_page_url
from autoscout.title_evidence import normalize_title_fields

NPR_FAILURE_STATUSES = {
    "captcha": ("skipped", "NPR live collection blocked by CAPTCHA-like response"),
    "blocked": ("skipped", "NPR live collection blocked by access controls"),
    "access-denied": ("skipped", "NPR live collection blocked by access-denied response"),
    "timeout": ("failed", "NPR live collection failed: request timed out"),
    "changed-markup": ("failed", "NPR live collection failed: response markup could not be parsed safely"),
    "malformed": ("failed", "NPR live collection failed: response metadata could not be parsed safely"),
}


@dataclass(frozen=True)
class NPRCollector:
    """Registered Collector adapter for NPR "Books We Love" sources.

    One instance is registered per method variant it owns
    (``npr-source-page``, ``npr-live``); ``variant`` is fixed at
    registration time and decides whether the response is read from a
    configured fixture or fetched/replayed live. The collector never
    inspects the source's ``extraction_method`` itself -- the dispatcher's
    registry lookup already guarantees it only runs for a source it owns."""

    variant: str

    def collect(
        self,
        config_path: Path,
        output_dir: Path,
        source: dict[str, Any],
        run_date: str,
    ) -> CollectorResult:
        if self.variant == "live":
            response_data, debug_metadata = live_npr_response(config_path, output_dir, source)
        else:
            response_data, debug_metadata = configured_npr_response(config_path, source)

        mentions = npr_mentions_from_response(response_data, source, run_date)
        if source.get("capture_normalized_mentions_file"):
            capture_normalized_mentions(config_path, output_dir, source, mentions)
        debug_metadata["collector_kind"] = "npr"
        return CollectorResult(
            mentions=mentions,
            raw_mention_count=len(mentions),
            valid_mention_count=len(mentions),
            debug_metadata=debug_metadata,
        )


def configured_npr_response(config_path: Path, source: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    debug_metadata = {
        "source_page_fixture_file": str(source.get("source_page_fixture_file", "")),
        "live_response_file": str(source.get("live_response_file", "")),
    }
    raw_path = source.get("source_page_fixture_file") or source.get("live_response_file")
    if not raw_path:
        return [], debug_metadata
    path = resolve_config_path(config_path, str(raw_path))
    debug_metadata["response_file"] = str(raw_path)
    return json.loads(path.read_text(encoding="utf-8")), debug_metadata


def live_npr_response(
    config_path: Path,
    output_dir: Path,
    source: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    if not source.get("enable_live"):
        raise _collector_error(
            "NPR live collection requires enable_live: true",
            status="skipped",
            debug_metadata={"enable_live": False},
        )
    if source.get("live_response_file"):
        data, debug_metadata = configured_npr_response(config_path, source)
        debug_metadata["live_fixture_replay"] = True
        return data, debug_metadata

    url = str(source.get("url", "")).strip()
    if not url:
        raise _collector_error("NPR live collection requires a configured source URL")
    request = Request(url, headers=collector_request_headers())
    try:
        with collectors_pkg().urlopen(request, timeout=float(source.get("timeout_seconds", 10))) as response:
            text = response.read(750_000).decode("utf-8", errors="replace")
    except TimeoutError:
        raise _collector_error("NPR live collection failed: request timed out")
    except HTTPError as exc:
        if exc.code in {401, 403, 429}:
            raise _collector_error(
                f"NPR live collection blocked by HTTP {exc.code}",
                status="skipped",
                debug_metadata={"http_status": str(exc.code)},
            )
        raise _collector_error(
            f"NPR live collection failed: HTTP {exc.code}",
            debug_metadata={"http_status": str(exc.code)},
        )
    except URLError as exc:
        raise _collector_error(f"NPR live collection failed: {exc.reason}")

    data = {"source_page": url, "html": text}
    if source.get("capture_live_response_file"):
        capture_live_response(config_path, output_dir, source, data)
    return data, {"live_fetch": True, "source_url": url}


def npr_mentions_from_response(data: Any, source: dict[str, Any], run_date: str) -> list[dict[str, Any]]:
    if isinstance(data, dict) and data.get("status"):
        status = str(data.get("status", "")).strip().lower()
        run_status, message = NPR_FAILURE_STATUSES.get(status, ("failed", "NPR live collection failed"))
        raise _collector_error(
            message,
            status=run_status,
            debug_metadata={"safe_debug_status": status},
        )
    entries = npr_entries(data)
    if not isinstance(entries, list):
        raise _collector_error("NPR source page entries must be a list")
    mentions: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise _collector_error(f"NPR entry {index + 1} must be an object")
        forbidden = FORBIDDEN_COLLECTOR_RESPONSE_FIELDS & set(entry)
        if forbidden:
            raise _collector_error(
                f"NPR source page stores forbidden copied text fields: {sorted(forbidden)}",
                raw_mention_count=len(entries),
            )
        title = str(entry.get("title", "")).strip()
        if not title:
            raise _collector_error(
                f"NPR entry {index + 1} missing required title",
                raw_mention_count=len(entries),
                valid_mention_count=len(mentions),
            )
        item_url = str(
            entry.get("item_url") or entry.get("source_url") or source_page_url(data) or source.get("url", "")
        ).strip()
        mention = {
            "title": title,
            "primary_authors": list(entry.get("authors") or entry.get("primary_authors") or []),
            "source_url": item_url,
            "evidence_url": item_url,
            "evidence_locator": str(entry.get("evidence_locator") or f"NPR source page entry for {title}.").strip(),
            "source_check_date": str(entry.get("source_check_date") or run_date),
            "signal_type": "media/curated list",
            "signal_strength": str(entry.get("signal_strength") or "major media list"),
            "evidence_note": str(
                entry.get("evidence_note") or f"NPR nonfiction staff pick; metadata-only entry for {title}."
            ).strip(),
            "candidate_category": str(entry.get("candidate_category") or entry.get("category", "")).strip(),
            "publisher": str(entry.get("publisher", "")).strip(),
            "publication_year": str(entry.get("publication_year", "")).strip(),
            "short_description": str(entry.get("short_description") or entry.get("summary", "")).strip(),
            "english_title": str(entry.get("english_title", "")).strip(),
            "english_title_type": entry.get("english_title_type", ""),
            "english_title_provenance": entry.get("english_title_provenance"),
            "english_author_display": str(entry.get("english_author_display", "")).strip(),
            "english_annotation": str(entry.get("english_annotation", "")).strip(),
            "public_data_status": str(entry.get("public_data_status", "")).strip(),
            "risk_evidence_url": str(entry.get("risk_evidence_url") or item_url).strip(),
            "risk_signal_scope": str(entry.get("risk_signal_scope", "")).strip(),
        }
        normalize_title_fields(mention, classify=True)
        mentions.append(mention)
    return mentions


def npr_entries(data: Any) -> Any:
    if isinstance(data, dict) and "html" in data:
        return parse_npr_html(str(data.get("html", "")), str(data.get("source_page", "")))
    if isinstance(data, str):
        return parse_npr_html(data, "")
    if isinstance(data, dict):
        return data.get("entries", data.get("mentions", []))
    return data


def parse_npr_html(markup: str, source_page: str) -> list[dict[str, Any]]:
    if likely_blocked_response(markup):
        raise _collector_error(
            "NPR live collection blocked by CAPTCHA-like response",
            status="skipped",
            debug_metadata={"safe_debug_status": "blocked"},
        )
    cards = re.findall(
        r"<article\b.*?</article>|<li\b.*?</li>",
        markup,
        flags=re.IGNORECASE | re.DOTALL,
    )
    entries = []
    for card in cards:
        title = npr_title_from_card(card)
        if not title:
            continue
        author = author_from_card(card)
        entries.append(
            {
                "title": title,
                "authors": [author] if author else [],
                "item_url": npr_item_url_from_card(card, source_page),
                "evidence_locator": f"NPR source page entry for {title}.",
            }
        )
    if not entries:
        raise _collector_error("NPR live collection failed: response markup could not be parsed safely")
    return entries


def npr_title_from_card(card: str) -> str:
    patterns = [
        r"<h[1-6]\b[^>]*>(.*?)</h[1-6]>",
        r"<a\b[^>]+href=[\"'][^\"']+[\"'][^>]*>(.*?)</a>",
    ]
    for pattern in patterns:
        match = re.search(pattern, card, flags=re.IGNORECASE | re.DOTALL)
        if match:
            title = clean_text(match.group(1))
            if title:
                return title
    return ""


def npr_item_url_from_card(card: str, source_page: str) -> str:
    match = re.search(
        r"<a\b[^>]+href=[\"']([^\"']+)[\"']",
        card,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return source_page
    return absolute_npr_url(match.group(1))


def absolute_npr_url(href: str) -> str:
    href = html.unescape(str(href).strip())
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return "https://www.npr.org" + href
    return href
