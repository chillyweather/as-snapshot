"""Issue #1057 part 3: annual and monthly sources are collected on one weekday.

The weekly run goes nightly. An annual or monthly source is captured and
extracted only on the collection weekday (Sunday by default) and is reported as
not due on the other nights. Its books stay in the review as they were.
"""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import pytest

import autoscout.source_collection as source_collection
from autoscout.annual_source_audit import ANNUAL_AUDIT_STAGE_NAME
from autoscout.collection_schedule import SCHEDULED_FREQUENCIES
from autoscout.collection_schedule import carried_forward_mentions
from autoscout.collection_schedule import collection_weekday
from autoscout.collection_schedule import is_source_due
from autoscout.collection_schedule import next_due_date
from autoscout.collection_schedule import not_due_source_entry
from autoscout.collection_schedule import source_check_frequency
from autoscout.config import resolve_weekly_portfolio_or_raise
from autoscout.config import validate_config
from autoscout.operator_notice import run_report
from autoscout.review_hiding import withdrawn_source_names
from autoscout.russian_workflow import render_workflow_result
from autoscout.scheduled_source_lists import STATE_SCHEDULED_SOURCE_LISTS
from autoscout.scheduled_source_lists import updated_scheduled_source_lists
from autoscout.source_collection import SourceCollectionResult
from autoscout.source_collection import prepare_codex_extraction_worklist
from autoscout.source_problem_notes import source_problem_notes
from autoscout.source_run_status import SOURCE_STATUS_NOT_DUE
from autoscout.source_run_status import is_stale_source_run
from autoscout.weekly_report import read_run_history
from autoscout.weekly_workflow import WorkflowOutcome
from autoscout.weekly_workflow import _capture_is_current
from autoscout.weekly_workflow import deterministic_adapters
from autoscout.weekly_workflow import run_weekly_workflow
from tests.test_issue_1057_nightly_run import _adapter
from tests.test_issue_1057_nightly_run import _repo_with_remote

SUNDAY = date(2026, 11, 15)
MONDAY = date(2026, 11, 16)
NEXT_SUNDAY = date(2026, 11, 22)

ANNUAL = "Test Prize"
WEEKLY = "Test Weekly Chart"
ANNUAL_URL = "https://example.test/prize"
WEEKLY_URL = "https://example.test/chart"


# ── The due rule ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("frequency", ["annual", "monthly", " Annual "])
def test_annual_and_monthly_sources_are_due_only_on_the_collection_weekday(frequency: str) -> None:
    assert is_source_due(frequency, SUNDAY)
    for offset in range(1, 7):
        day = date.fromordinal(SUNDAY.toordinal() + offset)
        assert not is_source_due(frequency, day), day


@pytest.mark.parametrize("frequency", ["weekly", "daily", "", "quarterly"])
def test_weekly_daily_and_unknown_frequencies_are_due_every_day(frequency: str) -> None:
    for offset in range(7):
        assert is_source_due(frequency, date.fromordinal(SUNDAY.toordinal() + offset))


def test_the_configured_weekday_is_respected() -> None:
    wednesday = date(2026, 11, 18)
    weekday = collection_weekday({"collection_weekday": "Wednesday"})
    assert weekday == "wednesday"
    assert is_source_due("annual", wednesday, weekday)
    assert not is_source_due("annual", SUNDAY, weekday)
    assert next_due_date(SUNDAY, weekday) == wednesday
    assert next_due_date(wednesday, weekday) == date(2026, 11, 25)


def test_the_default_weekday_is_sunday() -> None:
    assert collection_weekday({}) == "sunday"
    assert next_due_date(MONDAY) == NEXT_SUNDAY


@pytest.mark.parametrize("value", ["Sundy", "", 7, None])
def test_an_invalid_weekday_is_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        collection_weekday({"collection_weekday": value})
    errors = validate_config({"sources": [], "collection_weekday": value})
    assert any("collection_weekday" in error for error in errors), errors


def test_a_valid_weekday_passes_config_validation() -> None:
    assert validate_config({"sources": [], "collection_weekday": "saturday"}) == []


def test_carried_mentions_take_the_run_date_and_only_the_named_source() -> None:
    previous = [
        {"source_name": ANNUAL, "title": "A", "source_check_date": "2026-11-15"},
        {"source_name": WEEKLY, "title": "B", "source_check_date": "2026-11-15"},
        "not a mention",
    ]
    carried = carried_forward_mentions(previous, ANNUAL, "2026-11-16")
    assert carried == [{"source_name": ANNUAL, "title": "A", "source_check_date": "2026-11-16"}]
    assert previous[0]["source_check_date"] == "2026-11-15"


def test_not_due_is_neither_stale_nor_an_editor_problem() -> None:
    assert not is_stale_source_run(SOURCE_STATUS_NOT_DUE)
    run = {"source_name": ANNUAL, "status": SOURCE_STATUS_NOT_DUE, "reason": "not due"}
    assert source_problem_notes([run]) == []


# ── The weekly run ────────────────────────────────────────────────────────


class _Clock:
    def __init__(self, day: date) -> None:
        self.day = day

    def today(self) -> date:
        return self.day


def _book(title: str, author: str, *, family_signal: str) -> dict[str, Any]:
    slug = title.lower().replace(" ", "-")
    return {
        "title": title,
        "primary_authors": [author],
        "source_url": f"https://example.test/{slug}",
        "evidence_url": f"https://example.test/{slug}",
        "evidence_locator": f"{title} by {author}",
        "evidence_note": "Listed book.",
        "short_description": "A popular science book about everyday physics.",
        "candidate_category": "popular science",
        "signal_type": family_signal,
        "signal_strength": "shortlist",
    }


PRIZE_ONLY = _book("Prize Only Book", "Ada Prize", family_signal="award")
SHARED = _book("Shared Book", "Bea Shared", family_signal="award")
DROPPED = _book("Dropped Prize Book", "Cy Dropped", family_signal="award")
SHARED_ON_CHART = {**SHARED, "signal_type": "bestseller/ranking", "signal_strength": "top ranking"}
CHART_FIRST = _book("Chart First Book", "Dee Chart", family_signal="bestseller/ranking")
CHART_MONDAY = _book("Chart Monday Book", "Eve Monday", family_signal="bestseller/ranking")


def _source(name: str, url: str, family: str, frequency: str, slug: str) -> dict[str, Any]:
    return {
        "name": name,
        "family": family,
        "url": url,
        "access_status": "open",
        "extraction_method": "codex-extraction-replay",
        "check_frequency": frequency,
        "language": "en",
        "source_page_fixture_file": f"{slug}-capture.json",
        "extraction_response_file": f"{slug}-extraction.json",
    }


def _write_config(tmp_path: Path, *, annual_only: bool = False, alias_only: bool = False) -> Path:
    annual = _source(ANNUAL, ANNUAL_URL, "award", "annual", "prize")
    if alias_only:
        # The cadence declared only through the supported refresh_cadence alias.
        annual["refresh_cadence"] = annual.pop("check_frequency")
    sources = [annual]
    if not annual_only:
        sources.append(_source(WEEKLY, WEEKLY_URL, "bestseller/ranking", "weekly", "chart"))
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "run_date": SUNDAY.isoformat(),
                "readiness_purpose": "recurring-weekly",
                "editor_row_target": 1,
                "pilot_row_min": 1,
                "pilot_row_max": 50,
                "pilot_mode": True,
                "sources": sources,
            }
        ),
        encoding="utf-8",
    )
    links = " ".join(
        f'<a href="{book["evidence_url"]}">{book["title"]}</a> by {book["primary_authors"][0]}'
        for book in (PRIZE_ONLY, SHARED, DROPPED, CHART_FIRST, CHART_MONDAY)
    )
    for slug, url in (("prize", ANNUAL_URL), ("chart", WEEKLY_URL)):
        (tmp_path / f"{slug}-capture.json").write_text(
            json.dumps({"source_url": url, "html": f"<html><body>{links}</body></html>"}), encoding="utf-8"
        )
    return config_path


class _Run:
    """One state directory driven through several nights.

    Each night writes to ``output_dir``, which is the state directory unless a
    night names its own output directory.
    """

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        annual_only: bool = False,
        alias_only: bool = False,
        state_dir: Path | None = None,
    ) -> None:
        self.tmp_path = tmp_path
        self.config_path = _write_config(tmp_path, annual_only=annual_only, alias_only=alias_only)
        self.output_dir = tmp_path / "run"
        self.output_dir.mkdir()
        self.state_dir = state_dir
        self.fetched: list[str] = []
        self.extracted: list[str] = []
        self.fulfill_calls = 0
        original_page = source_collection.codex_source_page

        def recording_page(config_path: Path, output_dir: Path, source: dict[str, Any], **kwargs: Any) -> Any:
            self.fetched.append(str(source.get("name", "")))
            return original_page(config_path, output_dir, source, **kwargs)

        monkeypatch.setattr(source_collection, "codex_source_page", recording_page)

    def night(
        self,
        day: date,
        lists: dict[str, list[dict[str, Any]]],
        output_dir: Path | None = None,
        *,
        scheduled: bool = False,
    ) -> Any:
        if output_dir is not None:
            output_dir.mkdir(exist_ok=True)
            self.output_dir = output_dir
        self.fetched.clear()
        self.extracted.clear()
        self.fulfill_calls = 0
        run = self
        adapters = deterministic_adapters()
        adapters.clock = _Clock(day)
        codex = adapters.codex
        extracted = self.extracted

        class _RecordingCodex:
            """Extract each work item's list from *lists*, recording which sources it saw."""

            def fulfill(self, output_dir: Path, **kwargs: Any) -> dict[str, Any]:
                run.fulfill_calls += 1
                worklist = json.loads((output_dir / "codex-extraction-worklist.json").read_text(encoding="utf-8"))
                for item in worklist["work_items"]:
                    extracted.append(str(item["source_name"]))
                    (output_dir / item["expected_extraction_file"]).write_text(
                        json.dumps(
                            {
                                "source_name": item["source_name"],
                                "source_url": item["source_url"],
                                "operation_id": item["operation_id"],
                                "capture_sha256": item["capture_sha256"],
                                "entries": lists[item["source_name"]],
                            }
                        ),
                        encoding="utf-8",
                    )
                return codex.fulfill(output_dir, **kwargs)

            def fulfill_additional(self, output_dir: Path, **kwargs: Any) -> dict[str, Any]:
                return codex.fulfill_additional(output_dir, **kwargs)

        adapters.codex = _RecordingCodex()
        return run_weekly_workflow(
            self.config_path, self.output_dir, self.state_dir or self.output_dir, adapters=adapters, scheduled=scheduled
        )

    def json(self, name: str, default: Any = None) -> Any:
        path = self.output_dir / name
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default

    def history(self) -> dict[str, dict[str, Any]]:
        # The run's history copy, or the shards when the output is the sharded state directory (#1073).
        return {record["title"]: record for record in read_run_history(self.output_dir)}

    def review(self) -> str:
        return (self.output_dir / "weekly-review.md").read_text(encoding="utf-8")


def _source_names(record: dict[str, Any]) -> set[str]:
    return {str(evidence["source_name"]) for evidence in record.get("source_evidence", [])}


_KEPT_FIELDS = (
    "title",
    "primary_authors",
    "candidate_id",
    "independent_signal_families",
    "independent_signal_count",
    "priority_label",
    "primary_signal_strength",
    "editor_status",
    "russian_edition_status",
    "visible_risk_status",
)


def _kept(record: dict[str, Any]) -> dict[str, Any]:
    return {field: record.get(field) for field in _KEPT_FIELDS} | {"sources": _source_names(record)}


SUNDAY_LISTS = {ANNUAL: [PRIZE_ONLY, SHARED, DROPPED], WEEKLY: [SHARED_ON_CHART, CHART_FIRST]}


def test_an_annual_source_is_not_captured_or_extracted_on_a_weekday(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _Run(tmp_path, monkeypatch)
    sunday = run.night(SUNDAY, SUNDAY_LISTS)
    assert sorted(run.fetched) == sorted([ANNUAL, WEEKLY])
    assert sorted(run.extracted) == sorted([ANNUAL, WEEKLY])
    assert sunday.outcome == WorkflowOutcome.COMPLETED_NO_ATTENTION, sunday.attention_reasons

    monday = run.night(MONDAY, {WEEKLY: [SHARED_ON_CHART, CHART_FIRST, CHART_MONDAY]})

    assert run.fetched == [WEEKLY]
    assert run.extracted == [WEEKLY]
    worklist = run.json("codex-extraction-worklist.json")
    assert [item["source_name"] for item in worklist["work_items"]] == [WEEKLY]

    rows = {row["source_name"]: row for row in run.json("source-runs.json")}
    assert rows[ANNUAL]["status"] == SOURCE_STATUS_NOT_DUE
    assert rows[ANNUAL]["next_due_date"] == NEXT_SUNDAY.isoformat()
    assert rows[ANNUAL]["carried_mention_count"] == 3
    capture_rows = next(entry for entry in reversed(monday.log) if entry["stage"] == "capture")["detail"]["source_runs"]
    assert {row["source_name"]: row["status"] for row in capture_rows}[ANNUAL] == SOURCE_STATUS_NOT_DUE

    summary = run.json("run-summary.json")
    assert summary["capture_source_outcome_counts"]["not-due"] == 1
    assert summary["capture_source_outcome_counts"]["unknown"] == 0
    assert summary["failed_source_count"] == summary["skipped_source_count"] == 0
    assert summary["capture_ledger_status"] == "present"
    assert summary["reused_artifact_count"] == 0
    assert summary["source_volume_status"] == "at_or_above_target"
    assert summary["shortfall_reason"] == ""
    assert not [warning for warning in summary["major_warnings"] if "source" in warning], summary["major_warnings"]

    assert monday.outcome == WorkflowOutcome.COMPLETED_NO_ATTENTION, monday.attention_reasons
    assert monday.source_problems == []
    assert ANNUAL not in render_workflow_result(monday)
    review = run.review()
    assert ANNUAL not in "\n".join(line for line in review.splitlines() if "Источники" in line)
    assert "Не удалось собрать" not in review and "Пропущены" not in review
    html = (run.output_dir / "weekly-review.html").read_text(encoding="utf-8")
    assert "Не удалось собрать" not in html and "Пропущены" not in html


def test_the_annual_audit_runs_on_a_weekday(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _Run(tmp_path, monkeypatch)
    run.night(SUNDAY, SUNDAY_LISTS)
    monday = run.night(MONDAY, {WEEKLY: [SHARED_ON_CHART, CHART_FIRST]})
    assert [entry for entry in monday.log if entry["stage"] == ANNUAL_AUDIT_STAGE_NAME]


def test_books_stay_as_they_are_through_a_week(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _Run(tmp_path, monkeypatch)
    run.night(SUNDAY, SUNDAY_LISTS)
    after_sunday = run.history()
    assert _source_names(after_sunday["Shared Book"]) == {ANNUAL, WEEKLY}

    run.night(MONDAY, {WEEKLY: [SHARED_ON_CHART, CHART_FIRST, CHART_MONDAY]})
    after_monday = run.history()
    review = run.review()
    for title in ("Prize Only Book", "Shared Book", "Dropped Prize Book"):
        assert _kept(after_monday[title]) == _kept(after_sunday[title]), title
        assert title in review, title
    # A book two sources report keeps both signals while one is not due.
    assert set(after_monday["Shared Book"]["independent_signal_families"]) == {"award", "bestseller/ranking"}
    # The weekly source's new book is added normally.
    assert "Chart Monday Book" in after_monday
    assert "Chart Monday Book" not in after_sunday
    monday_candidates = {candidate["title"]: candidate for candidate in run.json("scouting-candidates.json")}
    assert monday_candidates["Chart Monday Book"]["update_delta"] == "new"
    for title in ("Prize Only Book", "Shared Book", "Dropped Prize Book"):
        assert monday_candidates.get(title, {}).get("update_delta", "unchanged") == "unchanged", title
    # Nothing is withdrawn because its only source was not due (#1025).
    assert run.json("hidden-from-review.json", []) == []
    assert withdrawn_source_names(run.json("codex-replay-config.json")["sources"]) == frozenset()
    assert validate_config(run.json("codex-replay-config.json")) == []

    run.night(NEXT_SUNDAY, {ANNUAL: [PRIZE_ONLY, SHARED], WEEKLY: [SHARED_ON_CHART, CHART_FIRST, CHART_MONDAY]})
    after_next_sunday = run.history()
    next_candidates = {candidate["title"]: candidate for candidate in run.json("scouting-candidates.json")}
    summary = run.json("run-summary.json")
    assert summary["update_delta_counts"]["new"] == 0
    assert summary["update_delta_counts"]["upgraded"] == 0
    assert summary["update_delta_counts"]["changed"] == 0
    for title in ("Prize Only Book", "Shared Book"):
        assert next_candidates.get(title, {}).get("update_delta", "unchanged") == "unchanged", title
        assert _kept(after_next_sunday[title]) == _kept(after_sunday[title]), title
    # A book the list really drops is no longer reported by it, as before.
    mentions = run.json("source-mentions.json")
    assert "Dropped Prize Book" not in {mention["title"] for mention in mentions}
    assert "Dropped Prize Book" not in next_candidates
    assert "Dropped Prize Book" in after_next_sunday


def test_a_weekday_capture_is_current_for_a_resumed_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _Run(tmp_path, monkeypatch)
    run.night(SUNDAY, SUNDAY_LISTS)
    adapters = deterministic_adapters()
    adapters.capture.prepare(run.config_path, run.output_dir, live=False, effective_date=MONDAY.isoformat())
    # A resumed run of the same operation reuses this capture instead of fetching again.
    assert _capture_is_current(run.output_dir, run.config_path, MONDAY)


def test_a_changed_schedule_makes_a_weekday_capture_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _Run(tmp_path, monkeypatch)
    adapters = deterministic_adapters()
    adapters.capture.prepare(run.config_path, run.output_dir, live=False, effective_date=MONDAY.isoformat())
    assert _capture_is_current(run.output_dir, run.config_path, MONDAY)

    config = json.loads(run.config_path.read_text(encoding="utf-8"))
    run.config_path.write_text(json.dumps({**config, "collection_weekday": "monday"}), encoding="utf-8")

    assert not _capture_is_current(run.output_dir, run.config_path, MONDAY)
    run.night(MONDAY, {ANNUAL: [PRIZE_ONLY], WEEKLY: [SHARED_ON_CHART, CHART_FIRST]})
    assert sorted(run.fetched) == sorted([ANNUAL, WEEKLY])
    rows = {row["source_name"]: row["status"] for row in run.json("source-runs.json")}
    assert rows[ANNUAL] != SOURCE_STATUS_NOT_DUE


def test_a_source_health_check_still_fetches_every_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _Run(tmp_path, monkeypatch)
    worklist = prepare_codex_extraction_worklist(run.config_path, tmp_path / "probe", MONDAY.isoformat())
    assert sorted(run.fetched) == sorted([ANNUAL, WEEKLY])
    assert SOURCE_STATUS_NOT_DUE not in {row["status"] for row in worklist.source_runs}


# ── The recorded lists (review round 1) ──────────────────────────────────


def _recorded(state_dir: Path) -> dict[str, Any]:
    path = state_dir / STATE_SCHEDULED_SOURCE_LISTS
    return json.loads(path.read_text(encoding="utf-8"))["sources"] if path.is_file() else {}


def test_a_separate_output_directory_keeps_the_annual_books(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "state"
    run = _Run(tmp_path, monkeypatch, state_dir=state_dir)
    run.night(SUNDAY, SUNDAY_LISTS, output_dir=tmp_path / "out-sunday")
    after_sunday = run.history()
    assert _recorded(state_dir)[ANNUAL]["collected_on"] == SUNDAY.isoformat()
    assert WEEKLY not in _recorded(state_dir)

    monday = run.night(MONDAY, {WEEKLY: [SHARED_ON_CHART, CHART_FIRST]}, output_dir=tmp_path / "out-monday")

    assert monday.outcome == WorkflowOutcome.COMPLETED_NO_ATTENTION, monday.attention_reasons
    rows = {row["source_name"]: row for row in run.json("source-runs.json")}
    assert rows[ANNUAL]["carried_from"] == "recorded-list"
    assert rows[ANNUAL]["carried_mention_count"] == 3
    after_monday = run.history()
    for title in ("Prize Only Book", "Shared Book", "Dropped Prize Book"):
        assert _kept(after_monday[title]) == _kept(after_sunday[title]), title
    assert set(after_monday["Shared Book"]["independent_signal_families"]) == {"award", "bestseller/ranking"}
    titles = {mention["title"] for mention in run.json("source-mentions.json") if mention["source_name"] == ANNUAL}
    assert titles == {"Prize Only Book", "Shared Book", "Dropped Prize Book"}


def test_a_failed_collection_keeps_the_recorded_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _Run(tmp_path, monkeypatch)
    run.night(SUNDAY, SUNDAY_LISTS)
    recorded = _recorded(run.output_dir)[ANNUAL]
    # Every row points at a page the capture does not link, so validation drops them all.
    unlinked = {**PRIZE_ONLY, "evidence_url": "https://example.test/elsewhere", "source_url": "https://example.test/x"}
    run.night(NEXT_SUNDAY, {ANNUAL: [unlinked], WEEKLY: [SHARED_ON_CHART, CHART_FIRST]})
    assert {row["source_name"]: row["status"] for row in run.json("source-runs.json")}[ANNUAL] == "failed"
    assert _recorded(run.output_dir)[ANNUAL] == recorded


def test_a_shorter_list_replaces_the_recorded_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _Run(tmp_path, monkeypatch)
    run.night(SUNDAY, SUNDAY_LISTS)
    run.night(NEXT_SUNDAY, {ANNUAL: [PRIZE_ONLY], WEEKLY: [SHARED_ON_CHART, CHART_FIRST]})
    recorded = _recorded(run.output_dir)[ANNUAL]
    assert recorded["collected_on"] == NEXT_SUNDAY.isoformat()
    assert [mention["title"] for mention in recorded["mentions"]] == ["Prize Only Book"]
    run.night(date(2026, 11, 23), {WEEKLY: [SHARED_ON_CHART, CHART_FIRST]})
    carried = {mention["title"] for mention in run.json("source-mentions.json") if mention["source_name"] == ANNUAL}
    assert carried == {"Prize Only Book"}


def test_a_source_without_a_recorded_list_falls_back_to_the_previous_output_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _Run(tmp_path, monkeypatch)
    run.night(SUNDAY, SUNDAY_LISTS)
    after_sunday = run.history()
    # As on the first weekday after this change ships: no list is recorded yet.
    (run.output_dir / STATE_SCHEDULED_SOURCE_LISTS).unlink()

    run.night(MONDAY, {WEEKLY: [SHARED_ON_CHART, CHART_FIRST]})

    rows = {row["source_name"]: row for row in run.json("source-runs.json")}
    assert rows[ANNUAL]["carried_from"] == "previous-output"
    assert rows[ANNUAL]["carried_mention_count"] == 3
    for title in ("Prize Only Book", "Shared Book", "Dropped Prize Book"):
        assert _kept(run.history()[title]) == _kept(after_sunday[title]), title
    recorded = _recorded(run.output_dir)[ANNUAL]
    assert recorded["collected_on"] == SUNDAY.isoformat()
    assert {mention["source_check_date"] for mention in recorded["mentions"]} == {SUNDAY.isoformat()}


def test_a_source_with_nothing_to_carry_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _Run(tmp_path, monkeypatch)
    monday = run.night(MONDAY, {WEEKLY: [SHARED_ON_CHART, CHART_FIRST]})
    rows = {row["source_name"]: row for row in run.json("source-runs.json")}
    assert rows[ANNUAL]["status"] == SOURCE_STATUS_NOT_DUE
    assert rows[ANNUAL]["carried_mention_count"] == 0
    assert rows[ANNUAL]["carried_from"] == "nothing"
    assert monday.source_problems == []


def test_an_annual_only_portfolio_carries_its_books_on_a_weekday(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _Run(tmp_path, monkeypatch, annual_only=True)
    sunday = run.night(SUNDAY, {ANNUAL: [PRIZE_ONLY, SHARED, DROPPED]})
    assert sunday.outcome == WorkflowOutcome.COMPLETED_NO_ATTENTION, sunday.attention_reasons
    after_sunday = run.history()

    monday = run.night(MONDAY, {})

    assert monday.outcome == WorkflowOutcome.COMPLETED_NO_ATTENTION, (monday.failed_stage, monday.attention_reasons)
    assert run.fetched == []
    assert run.fulfill_calls == 0
    assert run.json("codex-extraction-worklist.json")["work_items"] == []
    after_monday = run.history()
    for title in ("Prize Only Book", "Shared Book", "Dropped Prize Book"):
        assert _kept(after_monday[title]) == _kept(after_sunday[title]), title
        assert title in run.review(), title


def test_a_reused_collection_leaves_the_recorded_list() -> None:
    sunday_mention = {"source_name": ANNUAL, "title": "Prize Only Book", "source_check_date": SUNDAY.isoformat()}
    recorded = {ANNUAL: {"collected_on": SUNDAY.isoformat(), "mentions": [sunday_mention]}}
    older = {"source_name": ANNUAL, "title": "An Older Book", "source_check_date": NEXT_SUNDAY.isoformat()}
    result = SourceCollectionResult(source_mentions=[older], source_runs=[{"source_name": ANNUAL, "status": "reused"}])
    source = {"name": ANNUAL, "check_frequency": "annual"}

    assert updated_scheduled_source_lists(recorded, [source], result, NEXT_SUNDAY.isoformat()) == recorded

    fresh = SourceCollectionResult(
        source_mentions=[older], source_runs=[{"source_name": ANNUAL, "status": "collected"}]
    )
    replaced = updated_scheduled_source_lists(recorded, [source], fresh, NEXT_SUNDAY.isoformat())
    assert replaced[ANNUAL] == {"collected_on": NEXT_SUNDAY.isoformat(), "mentions": [older]}


def test_an_alias_only_annual_source_is_recorded_and_carried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "state"
    run = _Run(tmp_path, monkeypatch, alias_only=True, state_dir=state_dir)
    run.night(SUNDAY, SUNDAY_LISTS, output_dir=tmp_path / "out-sunday")
    replay = {source["name"]: source for source in run.json("codex-replay-config.json")["sources"]}
    assert replay[ANNUAL]["check_frequency"] == "annual"
    assert validate_config(run.json("codex-replay-config.json")) == []
    assert _recorded(state_dir)[ANNUAL]["collected_on"] == SUNDAY.isoformat()
    after_sunday = run.history()

    run.night(MONDAY, {WEEKLY: [SHARED_ON_CHART, CHART_FIRST]}, output_dir=tmp_path / "out-monday")

    assert run.fetched == [WEEKLY]
    rows = {row["source_name"]: row for row in run.json("source-runs.json")}
    assert rows[ANNUAL]["status"] == SOURCE_STATUS_NOT_DUE
    assert rows[ANNUAL]["carried_from"] == "recorded-list"
    assert rows[ANNUAL]["carried_mention_count"] == 3
    for title in ("Prize Only Book", "Shared Book", "Dropped Prize Book"):
        assert _kept(run.history()[title]) == _kept(after_sunday[title]), title


# ── The not-due replay entry is a valid source (review round 4) ──────────

_LIVE_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "current-weekly-pilot-live.json"


def _scheduled_live_sources() -> list[dict[str, Any]]:
    raw = json.loads(_LIVE_CONFIG.read_text(encoding="utf-8"))
    sources = resolve_weekly_portfolio_or_raise(raw, _LIVE_CONFIG)["sources"]
    return [
        source
        for source in sources
        if source.get("active", True) is not False and source_check_frequency(source) in SCHEDULED_FREQUENCIES
    ]


def test_every_scheduled_portfolio_source_has_a_valid_not_due_replay_entry() -> None:
    scheduled = _scheduled_live_sources()
    assert scheduled
    for source in scheduled:
        entry = not_due_source_entry(source, MONDAY, "sunday")
        errors = validate_config({"run_date": MONDAY.isoformat(), "sources": [entry]})
        assert errors == [], (source["name"], errors)


def test_a_capitalised_alias_only_cadence_gives_a_valid_entry_and_stays_annual() -> None:
    source = {**_source(ANNUAL, ANNUAL_URL, "award", "annual", "prize"), "refresh_cadence": "Annual"}
    del source["check_frequency"]
    assert not is_source_due(source_check_frequency(source), MONDAY)
    entry = not_due_source_entry(source, MONDAY, "sunday")
    assert validate_config({"run_date": MONDAY.isoformat(), "sources": [entry]}) == []
    assert source_check_frequency(entry) == "annual"


def test_a_nothing_due_night_completes_after_an_unfinished_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _Run(tmp_path, monkeypatch, annual_only=True)
    run.night(SUNDAY, {ANNUAL: [PRIZE_ONLY, SHARED, DROPPED]})
    # An earlier operation stopped with an unfinished extraction outcome.
    (run.output_dir / "codex-extraction-outcomes.json").write_text(
        json.dumps(
            {
                "outcomes": [
                    {
                        "source_name": ANNUAL,
                        "source_url": ANNUAL_URL,
                        "operation_id": "earlier-operation",
                        "outcome": "absent",
                        "terminal_class": "unfinished",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    monday = run.night(MONDAY, {})

    assert monday.outcome == WorkflowOutcome.COMPLETED_NO_ATTENTION, (monday.failed_stage, monday.attention_reasons)
    assert run.fulfill_calls == 0
    assert "Prize Only Book" in run.review()


def test_every_portfolio_source_would_give_a_valid_not_due_replay_entry() -> None:
    """A source switched to annual keeps a valid entry: each validator rule is checked once per contract family."""
    raw = json.loads(_LIVE_CONFIG.read_text(encoding="utf-8"))
    for source in resolve_weekly_portfolio_or_raise(raw, _LIVE_CONFIG)["sources"]:
        entry = not_due_source_entry(source, MONDAY, "sunday")
        errors = validate_config({"run_date": MONDAY.isoformat(), "sources": [entry]})
        assert errors == [], (source["name"], errors)


# ── With the nightly run of #1059 ─────────────────────────────────────────


def test_a_scheduled_weekday_run_skips_the_annual_source_and_reports_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _Run(tmp_path, monkeypatch)
    run.night(SUNDAY, SUNDAY_LISTS, scheduled=True)

    monday = run.night(MONDAY, {WEEKLY: [SHARED_ON_CHART, CHART_FIRST]}, scheduled=True)

    assert run.fetched == [WEEKLY]
    assert monday.outcome == WorkflowOutcome.COMPLETED_NO_ATTENTION, monday.attention_reasons
    assert monday.source_problems == []
    # A not-due source is no problem: the morning report says successful and names no source.
    report = run_report(monday, run_date=MONDAY.isoformat(), kirkus_attempt=None)
    assert report.subject == f"AutoScout: ночной поиск {MONDAY.isoformat()} — успешно"
    assert ANNUAL not in report.body and "Источники с проблемами" not in report.body


def test_a_modified_scheduled_source_lists_file_blocks_the_next_run(tmp_path: Path) -> None:
    repo, log = _repo_with_remote(tmp_path)
    lists = log.parent / STATE_SCHEDULED_SOURCE_LISTS
    lists.write_text('{"sources": {}, "version": 1}\n', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "lists"], cwd=repo, check=True, capture_output=True)
    lists.write_text('{"sources": {"X": {}}, "version": 1}\n', encoding="utf-8")
    adapter = _adapter(repo, log)

    result = adapter.preflight()
    adapter.release_guard()

    # Only an earlier run's log residue is set aside; this state file is not.
    assert result["status"] == "unsafe" and "dirty_working_tree" in result["reasons"]
    assert "log_residue_set_aside" not in result
