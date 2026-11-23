"""Issue #608: isolate a failed Boklista identity route from healthy sources."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from boklista_test_support import boklista_post_ranking_contract

import autoscout.source_collectors as source_collectors
from autoscout.source_collection import SourceCollection

BOKLISTA_URL = "https://example.test/wp-admin/admin-ajax.php"
BOKLISTA_IDENTITY_URL = "https://example.test/book-list/2026-33-generell-litteratur-voksne/"
BOKLISTA_NAME = "Bokhandlerforeningen Boklista - Generell litteratur voksne"
SIBLING_URL = "https://example.test/healthy-sibling"

BOKLISTA_SOURCE: dict[str, Any] = {
    "name": BOKLISTA_NAME,
    "family": "bestseller/ranking",
    "url": BOKLISTA_URL,
    "active": True,
    "access_status": "open",
    "check_frequency": "weekly",
    "extraction_method": "agent-extraction-live",
    "enable_live": True,
    "contract_family": "ranking-table",
    **boklista_post_ranking_contract(BOKLISTA_URL, expected_raw_row_count=1),
    "eligible_candidate_category": "adult nonfiction",
    "expected_signal_strength": "top ranking",
    "language": "no",
}


class _Response:
    def __init__(self, body: bytes, url: str) -> None:
        self.status = 200
        self.body = body
        self.url = url

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def geturl(self) -> str:
        return self.url

    def read(self, size: int) -> bytes:
        return self.body[:size]

    def close(self) -> None:
        return None


def _ranking_response() -> bytes:
    html = (
        '<div class="book-row-cover"><div class="book-row">'
        '<div class="col1"><h3 class="book-title">A bounded book</h3>'
        '<h3 class="author-name">A bounded author</h3></div>'
        '<div class="col2"><span class="isbn">9780200000017</span></div>'
        "</div></div>"
    )
    return json.dumps({"html": html, "last": True, "sel_year": 2026, "sel_week": 33}).encode()


def test_boklista_identity_failure_does_not_abort_a_healthy_sibling(monkeypatch: Any, tmp_path: Path) -> None:
    recorded = tmp_path / "sibling-mentions.json"
    recorded.write_text(
        json.dumps(
            {
                "mentions": [
                    {
                        "title": "Sibling book",
                        "primary_authors": ["Sibling author"],
                        "source_check_date": "2026-08-25",
                        "source_url": SIBLING_URL,
                        "evidence_url": f"{SIBLING_URL}/book",
                        "signal_type": "media/curated list",
                        "signal_strength": "major media list",
                        "evidence_note": "Healthy sibling evidence.",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    sibling = {
        "name": "Healthy sibling",
        "family": "media/curated list",
        "url": SIBLING_URL,
        "active": True,
        "access_status": "open",
        "check_frequency": "weekly",
        "extraction_method": "recorded",
        "recorded_mentions_file": str(recorded),
    }
    calls: list[tuple[str, str]] = []
    responses = {
        "POST": _Response(_ranking_response(), BOKLISTA_URL),
        "GET": _Response(b"<html><body><h1>Wrong category</h1></body></html>", BOKLISTA_IDENTITY_URL),
    }

    def opener(request: Any, timeout: float) -> _Response:
        del timeout
        method = request.get_method()
        calls.append((method, request.full_url))
        return responses[method]

    monkeypatch.setattr(source_collectors, "urlopen", opener)

    result = SourceCollection(tmp_path / "config.json", tmp_path / "output", "2026-08-25").collect(
        [BOKLISTA_SOURCE, sibling]
    )

    boklista_run, sibling_run = result.source_runs
    assert calls == [("POST", BOKLISTA_URL), ("GET", BOKLISTA_IDENTITY_URL)]
    assert boklista_run["source_name"] == BOKLISTA_NAME
    assert boklista_run["status"] == "failed"
    assert boklista_run["debug_metadata"]["safe_failure_status"] == "identity_route"
    assert sibling_run["source_name"] == "Healthy sibling"
    assert sibling_run["status"] == "collected"
    assert result.failed_source_count == 1
    assert [mention["title"] for mention in result.source_mentions] == ["Sibling book"]
