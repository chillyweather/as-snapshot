"""Issue #763: a ranking table that prints its week must not report a stale one.

On 2026-10-05 Publishers Weekly Hardcover Frontlist Nonfiction still showed the
chart for the week ended Sep. 19, 2026. It was sixteen days old and identical
to the chart collected on 2026-09-26, and the source reported 25 mentions as
``collected``. A fresh fetch the same day returned the same source note, so the
page itself was behind; the capture was not stale.

The source predates the declarative ranking contract. Its week is printed in a
source note under the table, the extractor copied it into ``source_week``, and
grounding confirmed the page said it. Nothing asked whether it was recent.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from autoscout.config import load_validated_config
from autoscout.config import validate_config
from autoscout.source_collection import SourceCollection
from autoscout.source_collection import prepare_codex_extraction_worklist
from autoscout.source_collectors import CollectorRunError
from autoscout.source_collectors import fetch_agent_source_page
from autoscout.source_metadata import portable_source_metadata
from autoscout.source_page_identity import validate_page_shape
from autoscout.source_week_freshness import source_statement_week
from autoscout.source_week_freshness import source_week_freshness_error
from autoscout.source_week_freshness import validate_source_week_freshness_declaration

ROOT = Path(__file__).resolve().parents[1]
NAME = "Publishers Weekly Hardcover Frontlist Nonfiction"
PAGE_FIXTURE = ROOT / "data/current-weekly-pilot/publishers-weekly-hardcover-frontlist-nonfiction-page.json"
EXTRACTION_FIXTURE = ROOT / "data/current-weekly-pilot/publishers-weekly-hardcover-frontlist-nonfiction-extraction.json"
FIXTURE_NOTE = (
    "Source: Circana BookScan, for the week ended Jul. 11, 2026; appearing in the Jul. 20, 2026, print edition of PW"
)
# The source note exactly as the page printed it on 2026-10-05.
STALE_NOTE = (
    "Source: Circana BookScan, for the week ended Sep. 19, 2026; appearing in the Sep. 28, 2026, print edition of PW"
)
STALE_REASON = (
    "page shape validation failed: source week ended 2026-09-19 is 16 days old on 2026-10-05; "
    "a ranking older than 14 days cannot be reported as current"
)


def _source() -> dict[str, Any]:
    config = load_validated_config(ROOT / "configs/current-weekly-pilot.json")
    return dict(next(value for value in config["sources"] if value["name"] == NAME))


def _page(note: str = STALE_NOTE, reference_date: str | None = "2026-10-05") -> dict[str, Any]:
    page = json.loads(PAGE_FIXTURE.read_text(encoding="utf-8"))
    assert FIXTURE_NOTE in page["html"]
    page["html"] = page["html"].replace(FIXTURE_NOTE, note)
    if reference_date is None:
        page.pop("reference_date", None)
    else:
        page["reference_date"] = reference_date
    return page


# ── The declaration ─────────────────────────────────────────────────────────


def test_the_source_declares_a_two_week_limit() -> None:
    source = _source()
    assert source["check_frequency"] == "weekly"
    assert source["source_week_max_age_days"] == 14
    assert validate_source_week_freshness_declaration(source) == ""


def test_the_limit_travels_with_the_source_into_replay() -> None:
    """Aged at capture and accepted unaged at finalize would be no guard."""
    assert portable_source_metadata(_source())["source_week_max_age_days"] == 14


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"source_week_max_age_days": 0}, "must be a positive integer"),
        ({"source_week_max_age_days": True}, "must be a positive integer"),
        ({"metadata_ranking_table": False}, "requires metadata_ranking_table true"),
        ({"ranking_row_selector": ".row"}, "declares ranking_reporting_period_max_age_days"),
    ],
)
def test_an_unusable_declaration_is_refused(overrides: dict[str, Any], fragment: str) -> None:
    source = {"metadata_ranking_table": True, "source_week_max_age_days": 14, **overrides}
    assert fragment in validate_source_week_freshness_declaration(source)


def test_config_validation_reports_the_declaration_error() -> None:
    source = {
        "name": "Chart",
        "url": "https://example.test/chart",
        "language": "en",
        "source_week_max_age_days": 14,
    }
    errors = validate_config({"run_date": "2026-10-05", "sources": [source]})
    assert any("source_week_max_age_days requires metadata_ranking_table true" in error for error in errors)


# ── Reading the week from the source note ───────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (STALE_NOTE, date(2026, 9, 19)),
        ("Source: Circana BookScan for Jul. 11, 2026", date(2026, 7, 11)),
        ("Source: Circana BookScan, for the week ended Sept. 5, 2026", date(2026, 9, 5)),
        ("Source: Circana BookScan, for the week ended May 2, 2026", date(2026, 5, 2)),
        ("Source: Circana BookScan, for the week ended September 19, 2026", date(2026, 9, 19)),
        ("source:  circana   bookscan, FOR THE WEEK ENDED SEP. 19, 2026", date(2026, 9, 19)),
    ],
)
def test_the_week_is_the_first_date_in_the_source_note(text: str, expected: date) -> None:
    assert source_statement_week(f"Rank Units {text} More from PW", {}) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Published Sep. 19, 2026. No source note at all.",
        "Source: Circana BookScan. All rights reserved.",
        "Source: Circana BookScan, for the week ended Sep. 31, 2026",
        "Sep. 19, 2026 comes before the note. Source: Circana BookScan.",
    ],
)
def test_a_note_without_a_readable_week_yields_none(text: str) -> None:
    assert source_statement_week(text, {}) is None


def test_a_declared_source_statement_is_the_one_read() -> None:
    source = {"ranking_source_statement": "Quelle: Beispiel"}
    text = "Source: Circana BookScan, Sep. 19, 2026. Quelle: Beispiel, Oct. 3, 2026."
    assert source_statement_week(text, source) == date(2026, 10, 3)


# ── Ageing the week ─────────────────────────────────────────────────────────


def test_the_chart_served_on_2026_10_05_is_refused_by_name() -> None:
    assert validate_page_shape(_source(), _page(), reference_date=date(2026, 10, 5)) == STALE_REASON


# Run date and printed week for committed weekly runs between 2026-08-13 and
# 2026-09-26. Each of them collected the current chart, at 7 to 14 days old.
@pytest.mark.parametrize(
    ("run_date", "week_ended"),
    [
        ("2026-08-13", "Aug. 1, 2026"),
        ("2026-08-15", "Aug. 1, 2026"),
        ("2026-08-17", "Aug. 8, 2026"),
        ("2026-08-21", "Aug. 8, 2026"),
        ("2026-08-22", "Aug. 15, 2026"),
        ("2026-08-27", "Aug. 15, 2026"),
        ("2026-08-30", "Aug. 22, 2026"),
        ("2026-09-03", "Aug. 22, 2026"),
        ("2026-09-07", "Aug. 29, 2026"),
        ("2026-09-09", "Aug. 29, 2026"),
        ("2026-09-13", "Sep. 5, 2026"),
        ("2026-09-16", "Sep. 5, 2026"),
        ("2026-09-20", "Sep. 12, 2026"),
        ("2026-09-26", "Sep. 19, 2026"),
    ],
)
def test_every_earlier_run_was_within_the_limit(run_date: str, week_ended: str) -> None:
    note = f"Source: Circana BookScan, for the week ended {week_ended}"
    assert validate_page_shape(_source(), _page(note), reference_date=date.fromisoformat(run_date)) == ""


def test_the_limit_is_inclusive() -> None:
    source = {"metadata_ranking_table": True, "source_week_max_age_days": 14}
    assert source_week_freshness_error(source, STALE_NOTE, date(2026, 10, 3)) == ""
    assert "15 days old" in source_week_freshness_error(source, STALE_NOTE, date(2026, 10, 4))


def test_a_declared_limit_is_never_passed_unaged() -> None:
    source = {"metadata_ranking_table": True, "source_week_max_age_days": 14}
    assert "requires a reference date" in source_week_freshness_error(source, STALE_NOTE, None)
    assert "no readable week" in source_week_freshness_error(source, "Source: Circana BookScan.", date(2026, 10, 5))
    assert "is after the reference date" in source_week_freshness_error(source, STALE_NOTE, date(2026, 9, 18))


def test_a_source_without_the_declaration_is_unaffected() -> None:
    source = {key: value for key, value in _source().items() if key != "source_week_max_age_days"}
    assert validate_page_shape(source, _page(), reference_date=date(2026, 10, 5)) == ""
    assert validate_page_shape(source, _page(reference_date=None)) == ""


# ── What the run records ────────────────────────────────────────────────────


def _config(tmp_path: Path, page: dict[str, Any]) -> Path:
    page_path = tmp_path / "page.json"
    page_path.write_text(json.dumps(page), encoding="utf-8")
    source = _source()
    source["source_page_fixture_file"] = str(page_path)
    source["extraction_response_file"] = str(EXTRACTION_FIXTURE.resolve())
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"run_date": "2026-10-05", "sources": [source]}), encoding="utf-8")
    return config_path


def test_capture_names_the_stale_chart_and_requests_no_extraction(tmp_path: Path) -> None:
    worklist = prepare_codex_extraction_worklist(_config(tmp_path, _page()), tmp_path / "out", "2026-10-05")

    assert worklist.work_items == []
    assert worklist.failed_source_count == 1
    run = worklist.source_runs[0]
    assert run["status"] == "failed"
    assert run["reason"] == STALE_REASON
    assert run["debug_metadata"]["safe_failure_status"] == "page_shape"


def test_capture_accepts_the_same_chart_while_it_is_current(tmp_path: Path) -> None:
    page = _page(reference_date="2026-09-26")
    worklist = prepare_codex_extraction_worklist(_config(tmp_path, page), tmp_path / "out", "2026-09-26")

    assert [run["status"] for run in worklist.source_runs] == ["codex-captured"]
    assert len(worklist.work_items) == 1


def test_replay_collection_contributes_no_mention_from_a_stale_chart(tmp_path: Path) -> None:
    page_path = tmp_path / "page.json"
    page_path.write_text(json.dumps(_page()), encoding="utf-8")
    source = _source()
    source["source_page_fixture_file"] = str(page_path)
    source["extraction_response_file"] = str(EXTRACTION_FIXTURE.resolve())
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-10-05").collect([source])

    run = result.source_runs[0]
    assert run["status"] == "failed"
    assert run["reason"] == STALE_REASON
    assert result.source_mentions == []


def test_a_replay_page_without_a_reference_date_is_refused(tmp_path: Path) -> None:
    page_path = tmp_path / "page.json"
    page_path.write_text(json.dumps(_page(FIXTURE_NOTE, reference_date=None)), encoding="utf-8")
    source = _source()
    source["source_page_fixture_file"] = str(page_path)
    source["extraction_response_file"] = str(EXTRACTION_FIXTURE.resolve())
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-07-17").collect([source])

    run = result.source_runs[0]
    assert run["status"] == "failed"
    assert "source week freshness requires a reference date" in run["reason"]


def test_the_live_fetch_refuses_the_stale_chart(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    body = _page()["html"].encode("utf-8")
    source = _source()

    class Response:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self, limit: int) -> bytes:
            return body[:limit]

        def geturl(self) -> str:
            return str(source["url"])

        def getheader(self, _name: str, default: str = "") -> str:
            return default

    monkeypatch.setattr("autoscout.source_collectors.urlopen", lambda *_args, **_kwargs: Response())
    with pytest.raises(CollectorRunError) as raised:
        fetch_agent_source_page(tmp_path / "config.json", tmp_path, source, reference_date=date(2026, 10, 5))
    assert raised.value.status == "failed"
    assert str(raised.value) == STALE_REASON

    page, _debug = fetch_agent_source_page(tmp_path / "config.json", tmp_path, source, reference_date=date(2026, 9, 26))
    assert page["reference_date"] == "2026-09-26"
