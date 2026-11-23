"""Issue #913: the review names the Kirkus books a run left out, not only how many."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from autoscout.html_review import build_html_review
from autoscout.json_io import write_json
from autoscout.review_rendering import render_review
from autoscout.source_page_identity import incomplete_coverage_from_run
from autoscout.source_page_identity import left_out_titles_text

SOURCE_NAME = "Kirkus Reviews Coming Soon Nonfiction"


def _run(titles: Any) -> dict[str, Any]:
    debug: dict[str, Any] = {
        "incomplete_ranking": True,
        "incomplete_ranking_warning": "Kirkus left out 2 listed books (missing_page_publication_date 2)",
        "raw_row_count": 112,
        "expected_raw_row_count": 114,
    }
    if titles is not None:
        debug["left_out_titles"] = titles
    return {"source_name": SOURCE_NAME, "status": "collected", "reason": "", "debug_metadata": debug}


def _write_snapshot(tmp_path: Path, runs: list[dict[str, Any]]) -> None:
    write_json(
        tmp_path / "scouting-candidates.json",
        [
            {
                "identity_key": "book one|alex example",
                "title": "Book One",
                "primary_authors": ["Alex Example"],
                "update_delta": "new",
                "priority_label": "A",
                "independent_signal_count": 1,
                "content_description": "Описание книги.",
                "source_evidence": [{"source_name": SOURCE_NAME, "evidence_url": "https://example.test/one"}],
            }
        ],
    )
    write_json(tmp_path / "run-summary.json", {"run_date": "2026-11-02", "editor_facing_count": 1})
    write_json(tmp_path / "source-runs.json", runs)


def test_coverage_carries_the_titles_a_run_recorded() -> None:
    coverage = incomplete_coverage_from_run(_run(["YOSEMITE FALLS", " ", 7, "BITTERSWEET EMPIRE"]))
    assert coverage is not None
    assert coverage["left_out_titles"] == ["YOSEMITE FALLS", "BITTERSWEET EMPIRE"]


def test_coverage_without_titles_keeps_its_earlier_shape() -> None:
    assert incomplete_coverage_from_run(_run(None)) == {
        "source_name": SOURCE_NAME,
        "observed_rows": 112,
        "expected_rows": 114,
    }
    assert left_out_titles_text({"left_out_titles": []}) == ""


def test_markdown_and_html_reviews_name_the_left_out_books(tmp_path: Path) -> None:
    _write_snapshot(tmp_path, [_run(["YOSEMITE FALLS", "BITTERSWEET EMPIRE"])])

    assert render_review(tmp_path) == 0

    expected = "не вошли: «YOSEMITE FALLS», «BITTERSWEET EMPIRE»"
    markdown = (tmp_path / "weekly-review.md").read_text(encoding="utf-8")
    assert "112 из 114" in markdown and expected in markdown
    html = (tmp_path / "weekly-review.html").read_text(encoding="utf-8")
    assert "112 из 114" in html and expected in html


def test_html_banner_escapes_a_title() -> None:
    html = build_html_review(
        [],
        {
            "total_count": 0,
            "incomplete_source_coverages": [
                {
                    "source_name": SOURCE_NAME,
                    "observed_rows": 1,
                    "expected_rows": 2,
                    "left_out_titles": ["<B>OLD</B>"],
                }
            ],
        },
    )
    assert "«&lt;B&gt;OLD&lt;/B&gt;»" in html
