"""Visible Risk Screen Coverage at the pipeline seam (issue #714).

Every case drives the full MVP pipeline on a fixture config whose Source
Mentions carry none of the bespoke-collector risk fields
(``public_data_status``, ``risk_signal_scope``, ``risk_evidence_url``), the
shape agent and Codex extraction produce. Assertions stay on what the
editor and the operator can observe: the Scouting Candidate, the
editor-facing CSV row, the run summary, Candidate History, and the
rendered review.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from autoscout.cli.weekly import run_mvp
from autoscout.review_rendering import render_review

RUN_DATE = "2026-06-15"


def _source(fixture_candidates: list[dict[str, Any]], *, notes: str = "") -> dict[str, Any]:
    return {
        "name": "Fixture Agent List",
        "family": "media/curated list",
        "url": "https://example.test/agent-list",
        "access_status": "open",
        "extraction_method": "fixture",
        "check_frequency": "weekly",
        "language": "en",
        "notes": notes,
        "fixture_candidates": fixture_candidates,
    }


def _book(title: str, **fields: Any) -> dict[str, Any]:
    slug = title.lower().replace(" ", "-")
    return {
        "title": title,
        "primary_authors": [f"{title.split()[0]} Example"],
        "source_url": f"https://example.test/books/{slug}",
        "signal_strength": "major media list",
        "candidate_category": "history",
        **fields,
    }


def _run(tmp_path: Path, fixture_candidates: list[dict[str, Any]], *, name: str = "runs") -> Path:
    config_path = tmp_path / "sources.json"
    config_path.write_text(
        json.dumps({"run_date": RUN_DATE, "sources": [_source(fixture_candidates)]}),
        encoding="utf-8",
    )
    output_dir = tmp_path / name
    run_mvp(config_path, output_dir)
    return output_dir


def _candidates_by_title(output_dir: Path) -> dict[str, dict[str, Any]]:
    candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
    return {candidate["title"]: candidate for candidate in candidates}


def _rows_by_title(output_dir: Path) -> dict[str, dict[str, str]]:
    with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
        return {row["Title"]: row for row in csv.DictReader(handle)}


def _run_summary(output_dir: Path) -> dict[str, Any]:
    return json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))


ORDINARY = _book(
    "The Patient Harbour",
    evidence_note="Included in a curated list of new history books.",
    short_description="A social history of a port city and its dock workers.",
)
AWARD = _book(
    "The Quiet Ledger",
    evidence_note="National Book Award for Nonfiction 2025 longlist",
)
CLEAR_RISK = _book(
    "The Microdosing Method",
    evidence_note="Public description says the book provides a microdosing protocol for psychoactive substances.",
    risk_signal_scope="book-content",
    risk_evidence_url="https://example.test/books/the-microdosing-method",
)
TITLE_ONLY = _book("Bare Title Only")
KEYWORD_HINT = _book(
    "Inside The Kremlin",
    evidence_note="Included in a curated list of new politics books.",
)


def test_ordinary_metadata_is_screened_and_reaches_no_visible_risk(tmp_path: Path) -> None:
    output_dir = _run(tmp_path, [ORDINARY])

    candidate = _candidates_by_title(output_dir)["The Patient Harbour"]
    assert candidate["visible_risk_screen_coverage"] == {
        "coverage": "screened",
        "evidence_fields": ["title", "short_description", "evidence_notes"],
        "reason": "Every configured category was checked against the title and a description.",
    }
    assert candidate["visible_risk_status"] == "no visible risk"
    assert candidate["visible_risk_category"] == ""
    row = _rows_by_title(output_dir)["The Patient Harbour"]
    assert row["Visible-risk status"] == "no visible risk"
    assert row["Risk comment"] == candidate["visible_risk_comment"]


def test_award_only_evidence_note_yields_no_category_and_no_status(tmp_path: Path) -> None:
    output_dir = _run(tmp_path, [AWARD, ORDINARY])

    candidate = _candidates_by_title(output_dir)["The Quiet Ledger"]
    assert candidate["visible_risk_category"] == ""
    assert candidate["visible_risk_status"] == ""
    assert candidate["visible_risk_screen_coverage"] == {
        "coverage": "partial",
        "evidence_fields": ["title", "evidence_notes"],
        "reason": "No description was available; configured categories were checked against the title and Evidence Notes only.",
    }
    assert _rows_by_title(output_dir)["The Quiet Ledger"]["Visible-risk status"] == ""


def test_generic_source_notes_do_not_make_a_title_only_book_screened(tmp_path: Path) -> None:
    config_path = tmp_path / "sources.json"
    config_path.write_text(
        json.dumps({"run_date": RUN_DATE, "sources": [_source([TITLE_ONLY], notes="Weekly curated list source.")]}),
        encoding="utf-8",
    )
    output_dir = tmp_path / "runs"
    run_mvp(config_path, output_dir)

    candidate = _candidates_by_title(output_dir)["Bare Title Only"]
    assert candidate["visible_risk_screen_coverage"]["coverage"] == "partial"
    assert candidate["visible_risk_status"] == ""


def test_russian_content_description_is_a_screening_input(tmp_path: Path) -> None:
    books = [
        _book("Quiet Harbour Read", content_description="Книга о жизни портового города и его жителей."),
        _book("Loud Harbour Read", content_description="Книга о том, как war меняет язык и память."),
    ]
    output_dir = _run(tmp_path, books)

    candidates = _candidates_by_title(output_dir)
    quiet = candidates["Quiet Harbour Read"]
    assert quiet["visible_risk_screen_coverage"]["coverage"] == "screened"
    assert quiet["visible_risk_screen_coverage"]["evidence_fields"] == ["title", "content_description"]
    assert quiet["visible_risk_status"] == "no visible risk"
    loud = candidates["Loud Harbour Read"]
    assert loud["visible_risk_category"] == "war/anti-war/Russian Armed Forces"
    assert loud["visible_risk_status"] == "text check required"


def test_credible_book_content_signal_stays_clear_visible_risk_in_the_csv(tmp_path: Path) -> None:
    output_dir = _run(tmp_path, [CLEAR_RISK, ORDINARY])

    candidate = _candidates_by_title(output_dir)["The Microdosing Method"]
    assert candidate["visible_risk_status"] == "clear visible risk"
    assert candidate["visible_risk_category"] == "drugs/microdosing/psychoactive substances"
    # A credible signal concludes on its own; coverage only says what was read.
    assert candidate["visible_risk_screen_coverage"]["coverage"] == "partial"
    row = _rows_by_title(output_dir)["The Microdosing Method"]
    assert row["Visible-risk status"] == "clear visible risk"
    assert row["Risk comment"] == candidate["visible_risk_comment"]
    assert "microdosing" in row["Risk comment"]
    assert row["Priority label"] == "D"


def test_keyword_hint_without_book_content_evidence_requires_a_text_check(tmp_path: Path) -> None:
    output_dir = _run(tmp_path, [KEYWORD_HINT])

    candidate = _candidates_by_title(output_dir)["Inside The Kremlin"]
    assert candidate["visible_risk_status"] == "text check required"
    assert candidate["visible_risk_category"] == "Russia/politics"
    assert candidate["visible_risk_comment"]
    assert _rows_by_title(output_dir)["Inside The Kremlin"]["Visible-risk status"] == "text check required"


def test_title_only_candidate_is_partial_with_a_reason_not_screened(tmp_path: Path) -> None:
    output_dir = _run(tmp_path, [TITLE_ONLY, ORDINARY])

    candidate = _candidates_by_title(output_dir)["Bare Title Only"]
    coverage = candidate["visible_risk_screen_coverage"]
    assert coverage["coverage"] == "partial"
    assert coverage["evidence_fields"] == ["title"]
    assert coverage["reason"] == (
        "No Content Description or Evidence Note was available; configured categories were checked against the title only."
    )
    assert candidate["visible_risk_status"] == ""


def test_no_candidate_is_left_with_an_empty_status_and_no_coverage(tmp_path: Path) -> None:
    output_dir = _run(tmp_path, [ORDINARY, AWARD, CLEAR_RISK, TITLE_ONLY, KEYWORD_HINT])

    for candidate in _candidates_by_title(output_dir).values():
        assert candidate["visible_risk_screen_coverage"]["coverage"] in {"screened", "partial"}


def test_run_summary_carries_coverage_counts(tmp_path: Path) -> None:
    output_dir = _run(tmp_path, [ORDINARY, AWARD, TITLE_ONLY])

    summary = _run_summary(output_dir)
    assert summary["visible_risk_screen_coverage_counts"] == {
        "screened": 1,
        "partial": 2,
        "not_run": 0,
        "failed": 0,
    }
    assert not any("Visible Risk screen" in warning for warning in summary["major_warnings"])


def test_run_where_nothing_concluded_raises_a_major_warning(tmp_path: Path) -> None:
    output_dir = _run(tmp_path, [TITLE_ONLY, _book("Another Bare Title")])

    summary = _run_summary(output_dir)
    assert summary["visible_risk_screen_coverage_counts"]["screened"] == 0
    assert "Visible Risk screen concluded for none of 2 editor-facing rows" in summary["major_warnings"]


def test_coverage_survives_into_candidate_history(tmp_path: Path) -> None:
    output_dir = _run(tmp_path, [ORDINARY, TITLE_ONLY])

    history = json.loads((output_dir / "candidate-history.json").read_text(encoding="utf-8"))
    coverage_by_title = {record["title"]: record["visible_risk_screen_coverage"]["coverage"] for record in history}
    assert coverage_by_title == {"The Patient Harbour": "screened", "Bare Title Only": "partial"}


def test_rendered_review_shows_coverage_in_russian_beside_the_risk_cell(tmp_path: Path) -> None:
    output_dir = _run(tmp_path, [ORDINARY, TITLE_ONLY])
    assert render_review(output_dir) == 0

    markdown = (output_dir / "weekly-review.md").read_text(encoding="utf-8")
    assert "| Риск | Проверка риска | Комментарий о риске |" in markdown
    assert "| Заметных рисков нет | Проверено по всем категориям |" in markdown
    assert "Проверено частично: есть только название" in markdown
    assert "**Проверка риска**: проверено по всем категориям — 1, частично — 1, не выполнялась — 0, с ошибкой — 0." in (
        markdown
    )
    html = (output_dir / "weekly-review.html").read_text(encoding="utf-8")
    assert "Проверка риска" in html
    assert "Проверено частично: есть только название" in html


def test_two_runs_of_the_same_config_are_byte_identical(tmp_path: Path) -> None:
    books = [ORDINARY, AWARD, CLEAR_RISK, TITLE_ONLY, KEYWORD_HINT]
    first = _run(tmp_path, books, name="first")
    second = _run(tmp_path, books, name="second")

    for artifact in ("scouting-candidates.json", "editor-facing.csv", "run-summary.json", "candidate-history.json"):
        assert (first / artifact).read_bytes() == (second / artifact).read_bytes(), artifact


def test_screened_negatives_are_not_unreviewed_but_generated_hints_are(tmp_path: Path) -> None:
    output_dir = _run(tmp_path, [ORDINARY, AWARD, KEYWORD_HINT])

    warnings = _run_summary(output_dir)["major_warnings"]
    assert "1 editor-facing rows have unreviewed Visible Risk status" in warnings


def test_a_failed_screen_raises_its_own_major_warning() -> None:
    from autoscout.reporting import review_metadata_warnings
    from autoscout.visible_risk_coverage import coverage_for_evidence
    from autoscout.visible_risk_coverage import failed_coverage

    screened = {"visible_risk_screen_coverage": coverage_for_evidence(["title", "evidence_notes"])}
    failed = {"visible_risk_screen_coverage": failed_coverage("TypeError")}

    assert "Visible Risk screen failed for 2 editor-facing rows" in review_metadata_warnings(
        [screened, failed, dict(failed)]
    )


def test_a_run_where_every_status_was_supplied_still_warns_that_nothing_concluded(tmp_path: Path) -> None:
    supplied = [
        {**ORDINARY, "visible_risk_status": "no visible risk", "visible_risk_status_source": "manual"},
        {**AWARD, "visible_risk_status": "no visible risk", "visible_risk_status_source": "manual"},
    ]
    output_dir = _run(tmp_path, supplied)

    summary = _run_summary(output_dir)
    assert summary["visible_risk_screen_coverage_counts"]["not_run"] == 2
    assert "Visible Risk screen concluded for none of 2 editor-facing rows" in summary["major_warnings"]


def test_post_enrichment_refresh_rescreens_with_the_new_description(tmp_path: Path) -> None:
    from autoscout.pipeline import refresh_post_enrichment_artifacts

    output_dir = _run(tmp_path, [AWARD])
    assert _run_summary(output_dir)["visible_risk_screen_coverage_counts"]["partial"] == 1

    # Description enrichment publishes a Russian Content Description after
    # the pipeline has screened.
    description = "Книга о жизни портового города и его жителей."
    for name in ("scouting-candidates.json", "candidate-history.json"):
        path = output_dir / name
        records = json.loads(path.read_text(encoding="utf-8"))
        for record in records:
            record["content_description"] = description
        path.write_text(json.dumps(records, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    result = refresh_post_enrichment_artifacts(tmp_path / "sources.json", output_dir)

    assert result["status"] == "ok"
    candidate = _candidates_by_title(output_dir)["The Quiet Ledger"]
    assert candidate["visible_risk_status"] == "no visible risk"
    assert candidate["visible_risk_screen_coverage"]["coverage"] == "screened"
    assert candidate["visible_risk_screen_coverage"]["evidence_fields"] == [
        "title",
        "content_description",
        "evidence_notes",
    ]
    history = json.loads((output_dir / "candidate-history.json").read_text(encoding="utf-8"))
    assert history[0]["visible_risk_status"] == "no visible risk"
    assert history[0]["visible_risk_screen_coverage"] == candidate["visible_risk_screen_coverage"]
    summary = _run_summary(output_dir)
    assert summary["visible_risk_screen_coverage_counts"]["screened"] == 1
    assert not any("Visible Risk screen" in warning for warning in summary["major_warnings"])

    assert render_review(output_dir) == 0
    assert _rows_by_title(output_dir)["The Quiet Ledger"]["Visible-risk status"] == "no visible risk"
