from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from autoscout.capture_bounds import CAPTURE_SERIALIZATION_HEADROOM_FACTOR
from autoscout.capture_bounds import CaptureKind
from autoscout.capture_bounds import CaptureWriteError
from autoscout.capture_bounds import retain_ranking_detail_pages
from autoscout.capture_bounds import write_bounded_capture
from autoscout.http_decoding import MAX_RESPONSE_BYTES
from autoscout.ranking_detail_route import RankingDetailRoute
from autoscout.ranking_evidence import RankingCaptureError
from autoscout.source_collectors.base import capture_extraction_response
from autoscout.source_collectors.base import capture_live_response


def cuspide_source(capture_path: Path) -> dict[str, Any]:
    return {
        "name": "Cúspide 100 más vendidos",
        "url": "https://cuspide.com/100-mas-vendidos/",
        "capture_live_response_file": str(capture_path),
        "max_response_bytes": 750_000,
        "ranking_detail_link_selector": ".name",
        "ranking_detail_url_pattern": r"https://cuspide\.com/producto/[^?#]+/?",
        "ranking_detail_scope_selector": ".product_meta",
        "ranking_detail_fields": ["isbn", "category"],
        "ranking_detail_max_pages": 100,
    }


def test_live_capture_bounds_cuspide_100_detail_pages_to_product_meta(tmp_path: Path) -> None:
    capture_path = tmp_path / "cuspide-capture.json"
    source = cuspide_source(capture_path)
    detail_pages = []
    for index in range(1, 101):
        detail_url = f"https://cuspide.com/producto/book-{index}/"
        detail_pages.append(
            {
                "source_page": detail_url,
                "detail_url": detail_url,
                "resolved_url": detail_url,
                "html": (
                    f"<article>{'outside scope content ' * 1_000}</article>"
                    f'<div class="product_meta"><span>ISBN: 9780306406157</span>'
                    f"<span>Category: Historia de América {index}</span></div>"
                ),
            }
        )

    capture_live_response(
        tmp_path / "config.json",
        tmp_path,
        source,
        {
            "source_page": source["url"],
            "resolved_url": source["url"],
            "html": '<h1 class="page-title">Los 100 más vendidos de la semana</h1>',
            "detail_pages": detail_pages,
        },
    )

    capture_bytes = capture_path.read_bytes()
    capture_limit = int(source["max_response_bytes"])
    assert len(capture_bytes) <= math.ceil(capture_limit * CAPTURE_SERIALIZATION_HEADROOM_FACTOR)
    capture = json.loads(capture_bytes)
    assert len(capture["detail_pages"]) == 100
    assert all('<div class="product_meta">' in detail["html"] for detail in capture["detail_pages"])
    assert all("outside scope content" not in detail["html"] for detail in capture["detail_pages"])


def test_live_capture_refuses_unresolved_detail_scope_without_output_file(tmp_path: Path) -> None:
    capture_path = tmp_path / "cuspide-capture.json"
    source = cuspide_source(capture_path)

    with pytest.raises(RankingCaptureError, match="detail scope capture failed"):
        capture_live_response(
            tmp_path / "config.json",
            tmp_path,
            source,
            {
                "source_page": source["url"],
                "html": "<h1>Los 100 más vendidos de la semana</h1>",
                "detail_pages": [
                    {
                        "source_page": "https://cuspide.com/producto/book-1/",
                        "detail_url": "https://cuspide.com/producto/book-1/",
                        "resolved_url": "https://cuspide.com/producto/book-1/",
                        "html": "<article>No declared scope</article>",
                    }
                ],
            },
        )

    assert not capture_path.exists()


def test_live_capture_refuses_detail_scope_over_existing_bound_without_output_file(tmp_path: Path) -> None:
    capture_path = tmp_path / "cuspide-capture.json"
    source = cuspide_source(capture_path)
    source["max_response_bytes"] = MAX_RESPONSE_BYTES * 2
    detail_url = "https://cuspide.com/producto/book-1/"
    retained_scope = "retained detail evidence " * 60_000

    with pytest.raises(RankingCaptureError, match="ranking rows capture exceeds the 1000000-byte limit"):
        capture_live_response(
            tmp_path / "config.json",
            tmp_path,
            source,
            {
                "source_page": source["url"],
                "html": "<h1>Los 100 más vendidos de la semana</h1>",
                "detail_pages": [
                    {
                        "source_page": detail_url,
                        "detail_url": detail_url,
                        "resolved_url": detail_url,
                        "html": f'<div class="product_meta">{retained_scope}</div>',
                    }
                ],
            },
        )

    assert not capture_path.exists()


def test_live_capture_refuses_oversized_aggregate_serialization_without_output_file(tmp_path: Path) -> None:
    capture_path = tmp_path / "cuspide-capture.json"
    source = cuspide_source(capture_path)
    source["max_response_bytes"] = 100_000
    detail_url = "https://cuspide.com/producto/book-1/"
    retained_scope = "retained detail evidence " * 10_000

    with pytest.raises(CaptureWriteError, match="150000-byte limit after serialization"):
        capture_live_response(
            tmp_path / "config.json",
            tmp_path,
            source,
            {
                "source_page": source["url"],
                "html": "<h1>Los 100 más vendidos de la semana</h1>",
                "detail_pages": [
                    {
                        "source_page": detail_url,
                        "detail_url": detail_url,
                        "resolved_url": detail_url,
                        "html": f'<div class="product_meta">{retained_scope}</div>',
                    }
                ],
            },
        )

    assert not capture_path.exists()


def test_extraction_capture_writes_compatible_bounded_schema(tmp_path: Path) -> None:
    capture_path = tmp_path / "extraction-capture.json"
    source = {
        "name": "Cúspide 100 más vendidos",
        "url": "https://cuspide.com/100-mas-vendidos/",
        "extraction_method": "agent-extraction-live",
        "capture_extraction_response_file": str(capture_path),
        "max_response_bytes": 10_000,
    }

    capture_extraction_response(
        tmp_path / "config.json",
        tmp_path,
        source,
        {"mentions": [{"title": "A book", "source_url": source["url"]}]},
        collection_mode="real-live",
    )

    assert capture_path.stat().st_size <= math.ceil(10_000 * CAPTURE_SERIALIZATION_HEADROOM_FACTOR)
    assert json.loads(capture_path.read_text(encoding="utf-8")) == {
        "collection_mode": "real-live",
        "source_url": source["url"],
        "source_name": source["name"],
        "extraction_method": source["extraction_method"],
        "response": {"mentions": [{"title": "A book", "source_url": source["url"]}]},
    }


def test_extraction_capture_refuses_oversized_serialization_without_output_file(tmp_path: Path) -> None:
    capture_path = tmp_path / "extraction-capture.json"
    source = {
        "name": "Cúspide 100 más vendidos",
        "url": "https://cuspide.com/100-mas-vendidos/",
        "extraction_method": "agent-extraction-live",
        "capture_extraction_response_file": str(capture_path),
        "max_response_bytes": 100,
    }

    with pytest.raises(CaptureWriteError, match="extraction capture exceeds"):
        capture_extraction_response(
            tmp_path / "config.json",
            tmp_path,
            source,
            {"mentions": [{"evidence_note": "large " * 1000}]},
        )

    assert not capture_path.exists()


def test_write_bounded_capture_normalizes_serialization_failure_without_output_file(tmp_path: Path) -> None:
    destination = tmp_path / "capture.json"

    with pytest.raises(CaptureWriteError, match="ranking capture could not be serialized: TypeError"):
        write_bounded_capture(
            {"html": object()},
            max_response_bytes=1_000,
            kind=CaptureKind.RANKING,
            destination=destination,
        )

    assert not destination.exists()


def test_write_bounded_capture_normalizes_filesystem_failure_with_path_free_diagnostic(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    destination = blocker / "nested" / "capture.json"

    with pytest.raises(CaptureWriteError, match="ranking capture could not be written") as excinfo:
        write_bounded_capture(
            {"html": "<h1>List</h1>"},
            max_response_bytes=1_000,
            kind=CaptureKind.RANKING,
            destination=destination,
        )

    assert str(tmp_path) not in str(excinfo.value)
    assert not destination.exists()


def test_malformed_detail_page_fails_closed_as_a_ranking_capture_error(tmp_path: Path) -> None:
    """A non-mapping detail page must fail closed, never be silently dropped."""
    source = cuspide_source(tmp_path / "capture.json")
    detail_route = RankingDetailRoute.from_source(source)

    with pytest.raises(RankingCaptureError, match="ranking detail page 1 is not a mapping"):
        retain_ranking_detail_pages(
            [
                {"source_page": "p", "html": "<div class='product_meta'>9781234567897</div>"},
                "not a mapping",
            ],
            detail_route,
        )
