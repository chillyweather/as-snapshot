"""Issue #384 — the review page states how much of the report is not this week's.

Reuse already reaches ``run-summary.json``, ``major_warnings``, the per-source
run status, and the spoken Russian completion summary. It does not reach
``weekly-review.html``, which is the artifact the editor actually opens and
works from for the rest of the week.

On the regenerated 2026-08-05 report, 7 of 24 contributing sources were served
from an artifact bound to an earlier operation, and the page said nothing. All
436 candidates were presented with equal authority.
"""

from __future__ import annotations

from pathlib import Path

from autoscout.html_review import build_html_review
from autoscout.json_io import write_json
from autoscout.review_rendering import render_review


def _row(**overrides):
    row = {
        "idx": 1,
        "identity_key": "book-one|alex-example",
        "title": "Book One",
        "authors": "Alex Example",
        "update_delta": "new",
        "priority": "A",
        "signal_count": "2",
        "russian": "not found in Russian",
        "russian_url": "",
        "risk": "no visible risk",
        "risk_comment": "",
        "editor_status": "",
        "editor_note": "",
        "description": "Описание книги.",
        "publisher": "Example Press",
        "publication_year": "2026",
        "why_entered": "Won a major award.",
        "signal_summary": "Award winner; two media lists.",
        "source": "National Book Awards",
        "source_url": "https://example.test/award",
        "taste_notes": "",
        "links": "",
    }
    row.update(overrides)
    return row


def _summary(**overrides):
    summary = {
        "delta_counts": {"new": 1, "upgraded": 0, "changed": 0, "unchanged": 0},
        "hidden_by_rule_count": 0,
        "editor_rows_count": 1,
        "total_count": 1,
    }
    summary.update(overrides)
    return summary


class ReviewPageReuseBannerTests:
    def test_states_the_reuse_share_when_sources_were_reused(self):
        """The 2026-08-05 shape: most of the portfolio is not this week's."""
        html = build_html_review(
            [_row()],
            _summary(reused_source_count=7, collecting_source_count=24),
        )

        assert "7 из 24" in html
        assert "предыдущего запуска" in html

    def test_says_nothing_when_every_source_was_freshly_collected(self):
        """A clean run reads as clean, so the warning keeps its weight."""
        html = build_html_review(
            [_row()],
            _summary(reused_source_count=0, collecting_source_count=24),
        )

        assert "предыдущего запуска" not in html

    def test_a_summary_predating_the_field_renders_unchanged(self):
        """Re-rendering an older report must not invent a warning or fail."""
        html = build_html_review([_row()], _summary())

        assert "предыдущего запуска" not in html
        assert "Всего книг: 1" in html


def _candidate(**overrides):
    candidate = {
        "identity_key": "book-one|alex-example",
        "title": "Book One",
        "primary_authors": ["Alex Example"],
        "update_delta": "new",
        "priority_label": "A",
        "independent_signal_count": 1,
        "russian_edition_status": "not found in Russian",
        "content_description": "Описание книги.",
        "source_evidence": [
            {
                "source_name": "PW Best Books 2025",
                "source_url": "https://example.test/pw",
                "evidence_url": "https://example.test/pw/book-one",
                "evidence_note": "Listed among the year's best.",
            }
        ],
    }
    candidate.update(overrides)
    return candidate


class RenderReviewCarriesReuseTests:
    """The counts must survive the trip from run-summary.json to the page."""

    def test_reuse_counts_reach_the_rendered_page(self, tmp_path: Path):
        write_json(tmp_path / "scouting-candidates.json", [_candidate()])
        write_json(
            tmp_path / "run-summary.json",
            {
                "run_date": "2026-08-05",
                "editor_facing_count": 1,
                "reused_artifact_count": 7,
                "freshly_collected_count": 17,
            },
        )

        assert render_review(tmp_path) == 0

        html = (tmp_path / "weekly-review.html").read_text(encoding="utf-8")
        # 7 reused against 17 fresh is 24 sources that contributed evidence.
        assert "7 из 24" in html
        assert "предыдущего запуска" in html

    def test_the_markdown_review_states_it_too(self, tmp_path: Path):
        """weekly-review.md is shared with the editor as well."""
        write_json(tmp_path / "scouting-candidates.json", [_candidate()])
        write_json(
            tmp_path / "run-summary.json",
            {
                "run_date": "2026-08-05",
                "editor_facing_count": 1,
                "reused_artifact_count": 7,
                "freshly_collected_count": 17,
            },
        )

        assert render_review(tmp_path) == 0

        markdown = (tmp_path / "weekly-review.md").read_text(encoding="utf-8")
        assert "7 из 24" in markdown
        assert "предыдущего запуска" in markdown

    def test_a_fully_fresh_run_leaves_both_artifacts_unmarked(self, tmp_path: Path):
        write_json(tmp_path / "scouting-candidates.json", [_candidate()])
        write_json(
            tmp_path / "run-summary.json",
            {
                "run_date": "2026-08-05",
                "editor_facing_count": 1,
                "reused_artifact_count": 0,
                "freshly_collected_count": 24,
            },
        )

        assert render_review(tmp_path) == 0

        for name in ("weekly-review.html", "weekly-review.md"):
            assert "предыдущего запуска" not in (tmp_path / name).read_text(encoding="utf-8")

    def test_an_older_report_without_the_fields_still_renders(self, tmp_path: Path):
        """Re-rendering a report from before this change must not fail."""
        write_json(tmp_path / "scouting-candidates.json", [_candidate()])
        write_json(tmp_path / "run-summary.json", {"run_date": "2026-07-28", "editor_facing_count": 1})

        assert render_review(tmp_path) == 0

        assert "предыдущего запуска" not in (tmp_path / "weekly-review.html").read_text(encoding="utf-8")
