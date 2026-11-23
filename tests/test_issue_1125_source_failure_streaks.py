"""Issue #1125: count failed attempts in a row per source, so a broken source stands out from one bad night."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from state_store_support import read_state
from state_store_support import seed_state

from autoscout.config import validate_config
from autoscout.doctor import EXIT_HEALTHY
from autoscout.doctor import EXIT_RECOVERABLE
from autoscout.doctor import DiagnosisClass
from autoscout.doctor import DoctorSeams
from autoscout.doctor import diagnose
from autoscout.doctor import exit_code_for
from autoscout.operator_notice import run_report
from autoscout.ranking_freshness import NOT_YET_REFRESHED_SKIP_STATUS
from autoscout.run_health import recurring_readiness
from autoscout.run_health import run_health
from autoscout.source_failure_streaks import _REASON_PHRASES
from autoscout.source_failure_streaks import DEFAULT_THRESHOLD
from autoscout.source_failure_streaks import DELIBERATE_SKIP_CODES
from autoscout.source_failure_streaks import RUN_SUMMARY_KEY
from autoscout.source_failure_streaks import THRESHOLD_CONFIG_KEY
from autoscout.source_failure_streaks import configured_threshold
from autoscout.source_failure_streaks import update_streaks
from autoscout.source_page_identity import BETWEEN_EDITIONS_SAFE_STATUS
from autoscout.source_problem_notes import _DEFAULT_REASONS
from autoscout.source_problem_notes import _REASON_RULES
from autoscout.state_store import EXPORT_CURRENT
from autoscout.state_store import SQLITE_BACKEND
from autoscout.state_store import BackendSelection
from autoscout.state_store import Collection
from autoscout.state_store import export_json
from autoscout.state_store import inspect_state_store
from autoscout.state_store import open_state_store
from autoscout.state_store.sqlite_schema import SCHEMA_VERSION
from autoscout.weekly_workflow import WeeklyWorkflowResult
from autoscout.weekly_workflow import WorkflowOutcome
from autoscout.weekly_workflow import attention_source_problems
from autoscout.weekly_workflow import deterministic_adapters
from autoscout.weekly_workflow import run_weekly_workflow
from autoscout.weekly_workflow import weekly_run_attention_reasons

BOOK_TITLE = "The Unmistakable Secret Book Title"
SOURCE = "Amazon Charts Most Read Nonfiction"


def _failed(name: str = SOURCE, code: str = "contract_violation") -> dict[str, Any]:
    return {
        "source_name": name,
        "status": "failed",
        "source_check_date": "2026-11-16",
        "reason": f"rejected all entries, including {BOOK_TITLE}",
        "debug_metadata": {"safe_failure_status": code},
    }


def _collected(name: str = SOURCE) -> dict[str, Any]:
    return {"source_name": name, "status": "collected", "reason": "", "source_check_date": "2026-11-16"}


def _runs(*nights: tuple[str, list[dict[str, Any]]], threshold: int = DEFAULT_THRESHOLD):
    """Apply each (run date, source runs) in turn; return the stored records and the last run's view."""
    records: list[dict[str, Any]] = []
    view: dict[str, Any] = {}
    for run_date, source_runs in nights:
        update = update_streaks(records, source_runs, run_date, threshold)
        records, view = update.records, update.view
    return {record["source_name"]: record for record in records}, view


# ── The counting rule ──────────────────────────────────────────────────────────


def test_three_failed_attempts_in_a_row_make_a_source_broken_with_its_last_success():
    records, view = _runs(
        ("2026-11-12", [_collected()]),
        ("2026-11-13", [_failed()]),
        ("2026-11-14", [_failed()]),
        ("2026-11-15", [_failed()]),
    )

    assert records[SOURCE]["consecutive_failures"] == 3
    assert records[SOURCE]["last_success_date"] == "2026-11-12"
    assert records[SOURCE]["streak_started"] == "2026-11-13"
    assert records[SOURCE]["last_reason_code"] == "contract_violation"
    (entry,) = view["failing"]
    assert entry["broken"] and entry["consecutive_failures"] == 3 and entry["last_success_date"] == "2026-11-12"


def test_one_failed_attempt_is_listed_but_not_broken():
    _records, view = _runs(("2026-11-12", [_collected()]), ("2026-11-13", [_failed()]))

    (entry,) = view["failing"]
    assert entry["counted"] and not entry["broken"] and entry["consecutive_failures"] == 1


@pytest.mark.parametrize(
    "idle",
    [
        {"source_name": SOURCE, "status": "not-due", "reason": "not due: a annual source is collected on Sunday"},
        {"source_name": SOURCE, "status": "reused", "reason": "served from an earlier operation"},
        {"source_name": SOURCE, "status": "inactive", "reason": ""},
        {"source_name": SOURCE, "status": "skipped", "reason": "paid: member site", "access_status": "paid"},
        {
            "source_name": SOURCE,
            "status": "skipped",
            "reason": "Kirkus capture awaits the background pre-collection pass",
        },
        {
            "source_name": SOURCE,
            "status": "skipped",
            "reason": "undated ranking list has not been refreshed",
            "debug_metadata": {"safe_failure_status": NOT_YET_REFRESHED_SKIP_STATUS},
        },
        {
            "source_name": SOURCE,
            "status": "skipped",
            "reason": "between editions: ...",
            "debug_metadata": {"safe_failure_status": BETWEEN_EDITIONS_SAFE_STATUS},
        },
    ],
    ids=[
        "not-due",
        "reused",
        "inactive",
        "configured-skip",
        "kirkus-deferred",
        "not-yet-refreshed",
        "between-editions",
    ],
)
def test_a_source_that_was_not_attempted_keeps_its_streak(idle):
    before, _view = _runs(("2026-11-01", [_collected()]), ("2026-11-02", [_failed()]), ("2026-11-03", [_failed()]))

    after, view = _runs(
        ("2026-11-01", [_collected()]),
        ("2026-11-02", [_failed()]),
        ("2026-11-03", [_failed()]),
        *[(f"2026-11-{day:02d}", [idle]) for day in range(4, 20)],
    )

    assert after == before
    assert all(not entry["broken"] for entry in view["failing"])


def test_a_blocked_page_is_a_failed_attempt_even_though_it_is_reported_as_skipped():
    blocked = {
        "source_name": SOURCE,
        "status": "skipped",
        "reason": "Codex extraction skipped: source or backend reported captcha",
        "debug_metadata": {"safe_failure_status": "source_blocked"},
    }

    records, view = _runs(*[(f"2026-11-0{day}", [blocked]) for day in (1, 2, 3)])

    assert records[SOURCE]["consecutive_failures"] == 3
    assert view["failing"][0]["broken"] and view["failing"][0]["reason_code"] == "source_blocked"


def test_two_failed_runs_on_one_date_count_once_and_the_next_date_counts_again():
    records, view = _runs(
        ("2026-11-16", [_failed()]),
        ("2026-11-16", [_failed(code="request_timeout")]),
        ("2026-11-16", [_failed()]),
        ("2026-11-17", [_failed()]),
    )
    assert records[SOURCE]["consecutive_failures"] == 2
    assert view["failing"][0]["consecutive_failures"] == 2 and not view["failing"][0]["broken"]


def test_a_second_run_on_the_same_date_updates_the_reason_code():
    records, _view = _runs(("2026-11-16", [_failed()]), ("2026-11-16", [_failed(code="request_timeout")]))

    assert records[SOURCE]["consecutive_failures"] == 1
    assert records[SOURCE]["last_reason_code"] == "request_timeout"


def test_a_success_resets_the_streak_and_the_recovery_is_reported_once():
    nights = [("2026-11-1" + str(day), [_failed()]) for day in range(4)]

    records, view = _runs(*nights, ("2026-11-15", [_collected()]))
    assert records[SOURCE]["consecutive_failures"] == 0
    assert records[SOURCE]["last_success_date"] == "2026-11-15"
    assert view["recovered"] == [{"source_name": SOURCE, "failed_attempts": 4, "last_failure_date": "2026-11-13"}]

    _records, next_view = _runs(*nights, ("2026-11-15", [_collected()]), ("2026-11-16", [_collected()]))
    assert next_view["recovered"] == []


def test_a_success_after_a_short_streak_is_no_recovery_to_report():
    records, view = _runs(("2026-11-13", [_failed()]), ("2026-11-14", [_failed()]), ("2026-11-15", [_collected()]))

    assert records[SOURCE]["consecutive_failures"] == 0
    assert view["recovered"] == []


def test_broken_sources_come_first_in_the_view():
    other = "Audible Bestselling Business & Careers Audiobooks"
    _records, view = _runs(
        ("2026-11-13", [_failed()]),
        ("2026-11-14", [_failed()]),
        ("2026-11-15", [_failed(other), _failed()]),
    )

    assert [entry["source_name"] for entry in view["failing"]] == [SOURCE, other]
    assert [entry["broken"] for entry in view["failing"]] == [True, False]


def test_the_records_hold_no_title_and_no_reason_text():
    records, view = _runs(("2026-11-13", [_failed()]), ("2026-11-14", [_failed()]))

    assert BOOK_TITLE not in json.dumps([records, view], ensure_ascii=False)
    assert "rejected" not in json.dumps([records, view], ensure_ascii=False)


def test_the_deliberate_skip_codes_and_the_reason_phrases_match_the_modules_that_write_them():
    assert DELIBERATE_SKIP_CODES == {NOT_YET_REFRESHED_SKIP_STATUS, BETWEEN_EDITIONS_SAFE_STATUS}
    assert {phrase for _needles, phrase in _REASON_RULES} | set(_DEFAULT_REASONS.values()) - {
        "данные предыдущего запуска"
    } <= _REASON_PHRASES


# ── The threshold is one config value with a default of 3 ──────────────────────


def test_the_threshold_defaults_to_three_and_is_one_config_value():
    assert DEFAULT_THRESHOLD == 3
    assert configured_threshold({"sources": []}) == 3
    assert configured_threshold({THRESHOLD_CONFIG_KEY: 2}) == 2

    _records, view = _runs(("2026-11-13", [_failed()]), ("2026-11-14", [_failed()]), threshold=2)
    assert view["failing"][0]["broken"] and view["threshold"] == 2


@pytest.mark.parametrize("value", [0, -1, "3", 2.5, True])
def test_a_threshold_that_is_not_a_whole_number_of_attempts_fails_validation(value):
    errors = validate_config({"sources": [], THRESHOLD_CONFIG_KEY: value})

    assert any(THRESHOLD_CONFIG_KEY in error for error in errors), errors


# ── Attention: below the threshold a failure is listed, not escalated ──────────


def _view(*entries: dict[str, Any], recovered: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"threshold": 3, "run_date": "2026-11-16", "failing": list(entries), "recovered": recovered or []}


def _entry(name: str = SOURCE, count: int = 1, *, status: str = "failed", counted: bool = True) -> dict[str, Any]:
    return {
        "source_name": name,
        "status": status,
        "reason_code": "contract_violation",
        "reason_ru": "не удалось собрать список",
        "counted": counted,
        "consecutive_failures": count,
        "last_success_date": "2026-11-13",
        "broken": counted and count >= 3,
    }


def test_a_single_failed_source_with_everything_else_clean_needs_no_attention():
    problems = [{"source_name": SOURCE, "status": "failed", "reason": "x"}]

    counted = attention_source_problems(problems, _view(_entry()))

    assert counted == []
    assert (
        weekly_run_attention_reasons(
            readiness_purpose="recurring-weekly",
            recurring_attention_required=recurring_readiness(
                Path("unused"),
                True,
                10,
                0,
                1,
                source_runs=[_failed()],
                source_failure_streaks=_view(_entry()),
            )["attention_required"],
            ready_for_editor=True,
            source_problem_count=len(counted),
            description_stage_problem_count=0,
        )
        == ()
    )


def test_a_broken_source_needs_attention_through_both_rules():
    problems = [{"source_name": SOURCE, "status": "failed", "reason": "x"}]
    view = _view(_entry(count=3))

    assert attention_source_problems(problems, view) == problems
    assert recurring_readiness(Path("unused"), True, 10, 0, 1, source_runs=[_failed()], source_failure_streaks=view)[
        "attention_required"
    ]


def test_other_source_problems_and_runs_without_streaks_need_attention_as_before():
    problems = [
        {"source_name": "Publishers Weekly Nonfiction Reviews", "status": "failed", "reason": "PW issue pass failed"},
        {"source_name": "BookPage Nonfiction", "status": "unusual_count", "reason": "3 instead of 20"},
        {"source_name": "Kirkus", "status": "incomplete", "reason": "short"},
    ]
    view = _view(_entry("Publishers Weekly Nonfiction Reviews"))

    assert attention_source_problems(problems, view, {"Publishers Weekly Nonfiction Reviews"}) == problems
    assert attention_source_problems(problems[:1], None) == problems[:1]
    assert recurring_readiness(Path("unused"), True, 10, 0, 1, source_runs=[_failed()])["attention_required"]


# ── The doctor and run-health ──────────────────────────────────────────────────


def _completed_output(tmp_path: Path, source_runs: list[dict[str, Any]], view: dict[str, Any] | None) -> Path:
    output = tmp_path / "out"
    output.mkdir()
    log = [
        {"stage": "capture", "status": "ok", "at": "2026-11-16T00:00:00Z"},
        {"stage": "repository-finalize", "status": "ok", "at": "2026-11-16T00:10:00Z"},
        {"stage": "terminal", "status": "completed/no-attention", "at": "2026-11-16T00:11:00Z"},
    ]
    summary: dict[str, Any] = {"run_date": "2026-11-16", "editor_facing_count": 5}
    if view is not None:
        summary[RUN_SUMMARY_KEY] = view
    pilot = {
        "run_date": "2026-11-16",
        "ready_for_editor": True,
        "readiness_basis": {"readiness_purpose": "recurring-weekly"},
    }
    for name, value in {
        "workflow-log.json": log,
        "source-runs.json": source_runs,
        "run-summary.json": summary,
        "pilot-status.json": pilot,
    }.items():
        (output / name).write_text(json.dumps(value), encoding="utf-8")
    (output / "weekly-review.html").write_text("<html>review</html>", encoding="utf-8")
    return output


def _diagnose(tmp_path: Path, output: Path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"run_date": "2026-11-16", "sources": []}), encoding="utf-8")
    seams = DoctorSeams(
        today=lambda: date(2026, 11, 16),
        repo_inspect=lambda _allowlist=None: {"status": "ok", "branch": "main"},
        codex_probe=lambda: {"codex_available": True, "codex_version": "0.1.0"},
        guard_status=lambda: {"guard_exists": False},
    )
    return diagnose(config, output, seams=seams)


def test_the_doctor_lists_a_first_failure_and_exits_healthy(tmp_path, capsys):
    output = _completed_output(tmp_path, [_failed()], _view(_entry()))

    report = _diagnose(tmp_path, output)

    assert report.diagnosis is DiagnosisClass.HEALTHY and exit_code_for(report) == EXIT_HEALTHY
    assert report.verdict == (
        "AutoScout в порядке — последний поиск завершён успешно, обзор доступен. "
        f"Сбои, пока не требующие внимания: {SOURCE} (не удалось собрать список; "
        "неудачных попыток подряд: 1, последний успех 2026-11-13)."
    )
    assert report.facts["failed_sources"] == [SOURCE] and report.facts["broken_sources"] == []

    assert run_health(output, "2026-11-16") == 0
    lines = capsys.readouterr().out.splitlines()
    assert "recurring_attention_required=false" in lines
    assert f"source_streak={SOURCE}|listed|1|2026-11-13|contract_violation" in lines
    assert "source_streak_threshold=3" in lines


def test_the_doctor_names_a_broken_source_first_and_exits_recoverable(tmp_path, capsys):
    other = "Audible Bestselling Business & Careers Audiobooks"
    view = _view(_entry(count=3), _entry(other, 1))
    output = _completed_output(tmp_path, [_failed(other), _failed()], view)

    report = _diagnose(tmp_path, output)

    assert report.diagnosis is DiagnosisClass.SOURCE_FAILURE and exit_code_for(report) == EXIT_RECOVERABLE
    assert report.verdict == (
        "Поиск завершён, но есть сломанные источники. "
        f"Не работают 3 и более попыток подряд: {SOURCE} (не удалось собрать список; "
        "неудачных попыток подряд: 3, последний успех 2026-11-13). "
        f"Сбои, пока не требующие внимания: {other} (не удалось собрать список; "
        "неудачных попыток подряд: 1, последний успех 2026-11-13). Остальные результаты доступны."
    )
    assert report.facts["broken_sources"] == [SOURCE] and report.facts["streak_threshold"] == 3

    assert run_health(output, "2026-11-16") == 0
    lines = capsys.readouterr().out.splitlines()
    assert "recurring_attention_required=true" in lines
    assert f"source_streak={SOURCE}|broken|3|2026-11-13|contract_violation" in lines


def test_the_doctor_reports_a_recovery_in_the_healthy_verdict(tmp_path, capsys):
    view = _view(recovered=[{"source_name": SOURCE, "failed_attempts": 4, "last_failure_date": "2026-11-15"}])
    output = _completed_output(tmp_path, [_collected()], view)

    report = _diagnose(tmp_path, output)

    assert report.diagnosis is DiagnosisClass.HEALTHY
    assert report.verdict.endswith(f"Снова работают: {SOURCE} (до этого неудачных попыток подряд: 4).")
    assert run_health(output, "2026-11-16") == 0
    assert f"source_recovered={SOURCE}|4" in capsys.readouterr().out.splitlines()


def test_a_run_made_before_the_streaks_keeps_the_old_verdict(tmp_path):
    output = _completed_output(tmp_path, [_failed()], None)

    report = _diagnose(tmp_path, output)

    assert report.diagnosis is DiagnosisClass.SOURCE_FAILURE and exit_code_for(report) == EXIT_RECOVERABLE


# ── The morning report ─────────────────────────────────────────────────────────


def _result(outcome: WorkflowOutcome, view: dict[str, Any], problems: list[dict[str, Any]], **extra: Any):
    return WeeklyWorkflowResult(
        outcome=outcome,
        delta_counts={"new": 2},
        source_problems=problems,
        run_summary={RUN_SUMMARY_KEY: view},
        **extra,
    )


def test_the_morning_report_of_a_first_failure_says_successful_and_lists_the_source():
    problems = [{"source_name": SOURCE, "status": "failed", "reason": f"rejected {BOOK_TITLE}", "title": BOOK_TITLE}]
    result = _result(WorkflowOutcome.COMPLETED_NO_ATTENTION, _view(_entry()), problems)

    report = run_report(result, run_date="2026-11-16", kirkus_attempt=None)

    assert report.subject == "AutoScout: ночной поиск 2026-11-16 — успешно"
    assert report.body.splitlines()[:3] == [
        "Ночной поиск 2026-11-16 завершён успешно.",
        "Новых книг: 2. Обновлено книг: 0.",
        f"Сбои, пока не требующие внимания: {SOURCE} (не удалось собрать список, contract_violation; "
        "неудачных попыток подряд: 1, последний успех 2026-11-13).",
    ]
    assert BOOK_TITLE not in report.subject + report.body and "rejected" not in report.body


def test_the_morning_report_names_a_broken_source_before_the_other_problems():
    other = "BookPage Nonfiction"
    problems = [
        {"source_name": SOURCE, "status": "failed", "reason": "x"},
        {"source_name": other, "status": "unusual_count", "editor_detail": BOOK_TITLE},
    ]
    result = _result(
        WorkflowOutcome.COMPLETED_ATTENTION_REQUIRED, _view(_entry(count=3)), problems, attention_reasons=("sources",)
    )

    body = run_report(result, run_date="2026-11-16", kirkus_attempt=None).body.splitlines()

    assert body[0] == "Ночной поиск 2026-11-16 завершён, но требует внимания: источники."
    assert body[2:4] == [
        f"Не работают 3 и более попыток подряд: {SOURCE} (не удалось собрать список, contract_violation; "
        "неудачных попыток подряд: 3, последний успех 2026-11-13).",
        f"Источники с проблемами: {other} (необычное число книг).",
    ]
    assert BOOK_TITLE not in "\n".join(body)


def test_the_morning_report_names_a_recovery_once():
    view = _view(recovered=[{"source_name": SOURCE, "failed_attempts": 5, "last_failure_date": "2026-11-15"}])
    result = _result(WorkflowOutcome.COMPLETED_NO_ATTENTION, view, [])

    body = run_report(result, run_date="2026-11-16", kirkus_attempt=None).body

    assert f"Снова работают: {SOURCE} (до этого неудачных попыток подряд: 5)." in body


def test_a_problem_a_pass_reported_is_not_called_one_that_needs_no_attention():
    pw = "Publishers Weekly Nonfiction Reviews"
    problems = [{"source_name": pw, "status": "failed", "reason": "PW issue pass failed"}]
    result = _result(
        WorkflowOutcome.COMPLETED_ATTENTION_REQUIRED,
        _view(_entry(pw)),
        problems,
        attention_reasons=("sources",),
        pass_problem_sources=(pw,),
    )

    body = run_report(result, run_date="2026-11-16", kirkus_attempt=None).body

    assert f"Источники с проблемами: {pw} (не удалось проверить)." in body
    assert "пока не требующие внимания" not in body


# ── The State Store: a database built before #1125 ─────────────────────────────


def _selection(tmp_path: Path) -> BackendSelection:
    return BackendSelection(SQLITE_BACKEND, "config", tmp_path / "databases" / "state.sqlite3")


def _version_one_database(tmp_path: Path) -> tuple[Path, BackendSelection]:
    """A state directory and its database as AutoScout left them before schema version 2, export current."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    selection = _selection(tmp_path)
    open_state_store(state_dir, selection).replace(Collection.CANDIDATE_VISIBILITY, [])
    export_json(state_dir, selection)
    with sqlite3.connect(selection.database) as conn:
        conn.execute("DROP TABLE source_failure_streaks")
        conn.execute("DELETE FROM schema_version WHERE version = 2")
    return state_dir, selection


def test_a_database_built_before_the_streaks_still_reads_as_current_and_is_left_unchanged(tmp_path):
    state_dir, selection = _version_one_database(tmp_path)
    before = selection.database.read_bytes()

    inspection = inspect_state_store(state_dir, selection)

    assert inspection.consistent and inspection.export_status == EXPORT_CURRENT
    assert inspection.schema_version == 1 and inspection.problem == ""
    assert selection.database.read_bytes() == before


def test_the_first_run_migrates_the_old_database_and_its_export_stays_current(tmp_path):
    state_dir, selection = _version_one_database(tmp_path)
    store = open_state_store(state_dir, selection)

    assert store.read(Collection.SOURCE_FAILURE_STREAKS) == []
    assert inspect_state_store(state_dir, selection).schema_version == SCHEMA_VERSION
    assert inspect_state_store(state_dir, selection).export_status == EXPORT_CURRENT

    update = update_streaks([], [_failed()], "2026-11-17")
    store.replace(Collection.SOURCE_FAILURE_STREAKS, update.records)
    export_json(state_dir, selection)

    assert inspect_state_store(state_dir, selection).export_status == EXPORT_CURRENT
    assert json.loads((state_dir / "source-failure-streaks.json").read_text(encoding="utf-8")) == update.records


def test_the_streaks_round_trip_through_the_state_store(tmp_path):
    state_dir = tmp_path / "state"
    records = update_streaks([], [_failed("B source"), _collected("A source")], "2026-11-17").records
    seed_state(state_dir, source_failure_streaks=list(reversed(records)))

    assert read_state(state_dir, Collection.SOURCE_FAILURE_STREAKS) == records


# ── The weekly run ─────────────────────────────────────────────────────────────


def _fixture_run(tmp_path: Path, *, entries: bool) -> tuple[Path, Path, Any]:
    from tests.test_weekly_workflow import _write_capture_fixture
    from tests.test_weekly_workflow import _write_codex_fixture_config
    from tests.test_weekly_workflow import _write_extraction_fixture

    config = _write_codex_fixture_config(tmp_path)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    _write_capture_fixture(tmp_path, output_dir)
    adapters = deterministic_adapters()
    adapters.capture.prepare(config, output_dir, live=False)
    extraction = _write_extraction_fixture(output_dir)
    if not entries:
        document = json.loads(extraction.read_text(encoding="utf-8"))
        document["entries"] = []
        extraction.write_text(json.dumps(document), encoding="utf-8")
    return config, output_dir, adapters


def _streak(name: str, count: int, last_failure: str) -> dict[str, Any]:
    return {
        "source_name": name,
        "consecutive_failures": count,
        "streak_started": last_failure,
        "last_failure_date": last_failure,
        "last_success_date": "1999-01-01",
        "last_reason_code": "healthy_empty",
    }


def test_a_weekly_run_with_one_failed_source_completes_without_attention(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path, entries=False)
    state_dir = tmp_path / "state"

    result = run_weekly_workflow(config, output_dir, state_dir, adapters=adapters)

    assert result.outcome is WorkflowOutcome.COMPLETED_NO_ATTENTION, (result.failed_stage, result.attention_reasons)
    assert [problem["source_name"] for problem in result.source_problems] == ["Test Source"]
    (stored,) = read_state(state_dir, Collection.SOURCE_FAILURE_STREAKS)
    assert stored["source_name"] == "Test Source" and stored["consecutive_failures"] == 1
    summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
    assert summary[RUN_SUMMARY_KEY]["failing"][0]["broken"] is False


def test_a_weekly_run_that_reaches_the_threshold_needs_attention(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path, entries=False)
    state_dir = tmp_path / "state"
    seed_state(state_dir, source_failure_streaks=[_streak("Test Source", 2, "1999-12-30")])

    result = run_weekly_workflow(config, output_dir, state_dir, adapters=adapters)

    assert result.outcome is WorkflowOutcome.COMPLETED_ATTENTION_REQUIRED
    assert "sources" in result.attention_reasons
    (stored,) = read_state(state_dir, Collection.SOURCE_FAILURE_STREAKS)
    assert stored["consecutive_failures"] == 3
    body = run_report(result, run_date="2026-11-16", kirkus_attempt=None).body
    assert "Не работают 3 и более попыток подряд: Test Source" in body


def test_a_weekly_run_after_a_long_streak_reports_the_recovery(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path, entries=True)
    state_dir = tmp_path / "state"
    seed_state(state_dir, source_failure_streaks=[_streak("Test Source", 6, "1999-12-30")])

    result = run_weekly_workflow(config, output_dir, state_dir, adapters=adapters)

    assert result.outcome.value.startswith("completed/"), result.failed_stage
    (stored,) = read_state(state_dir, Collection.SOURCE_FAILURE_STREAKS)
    assert stored["consecutive_failures"] == 0 and stored["last_success_date"] is not None
    body = run_report(result, run_date="2026-11-16", kirkus_attempt=None).body
    assert "Снова работают: Test Source (до этого неудачных попыток подряд: 6)." in body


# ── A widespread failure is never noise ────────────────────────────────────────


def _night(failed: int, collected: int) -> list[dict[str, Any]]:
    return [_failed(f"Failing {index}") for index in range(failed)] + [
        _collected(f"Working {index}") for index in range(collected)
    ]


@pytest.mark.parametrize(
    ("failed", "collected", "widespread"),
    [(28, 1, True), (2, 1, True), (2, 2, False), (1, 0, False), (3, 17, False)],
)
def test_more_than_half_of_the_attempts_failing_is_widespread(failed, collected, widespread):
    not_due = {"source_name": "Annual Prize", "status": "not-due", "reason": "not due"}

    _records, view = _runs(("2026-11-10", [*_night(failed, collected), not_due]))

    assert view["widespread"] is widespread
    assert view["attempted"] == failed + collected and view["failed_attempts"] == failed


def test_a_widespread_failure_needs_attention_although_every_streak_is_short(tmp_path):
    _records, view = _runs(("2026-11-10", _night(3, 1)))
    problems = [{"source_name": f"Failing {index}", "status": "failed", "reason": "x"} for index in range(3)]

    assert attention_source_problems(problems, view) == problems
    output = _completed_output(tmp_path, _night(3, 1), view)
    report = _diagnose(tmp_path, output)
    assert report.diagnosis is DiagnosisClass.SOURCE_FAILURE and exit_code_for(report) == EXIT_RECOVERABLE
    assert report.verdict.startswith(
        "Поиск завершён, но сбор источников массово не удался. Не удалось собрать 3 из 4 источников этого запуска: "
        "похоже на общий сбой, а не на отдельные источники. Другие сбои этого запуска: Failing 0 ("
    )
    assert report.facts["widespread_failure"] is True

    result = _result(WorkflowOutcome.COMPLETED_ATTENTION_REQUIRED, view, problems, attention_reasons=("sources",))
    body = run_report(result, run_date="2026-11-10", kirkus_attempt=None).body.splitlines()
    assert body[2].startswith("Не удалось собрать 3 из 4 источников этого запуска")
    assert body[3].startswith("Другие сбои этого запуска: Failing 0 (не удалось собрать список, contract_violation;")


# ── Review round 1 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "skip",
    [
        {"source_name": SOURCE, "status": "skipped", "reason": "HTTP 403", "debug_metadata": {"http_status": 403}},
        {"source_name": SOURCE, "status": "skipped", "reason": "agent extraction needs a credential"},
    ],
    ids=["http-403-without-code", "missing-credential"],
)
def test_a_skip_that_is_not_deliberate_is_a_failed_attempt_with_or_without_a_code(skip):
    records, view = _runs(*[(f"2026-11-0{day}", [skip]) for day in (1, 2, 3)])

    assert records[SOURCE]["consecutive_failures"] == 3
    assert view["failing"][0]["broken"] and view["failing"][0]["reason_code"] == "source_skipped"


def test_a_pass_problem_asks_for_attention_in_the_doctor_and_run_health_too(tmp_path, capsys):
    kirkus = "Kirkus Reviews Coming Soon Nonfiction"
    _records, view = _runs(("2026-11-10", [_failed(kirkus), *_night(0, 5)]))
    view = update_streaks([], [_failed(kirkus), *_night(0, 5)], "2026-11-10", 3, {kirkus}).view
    assert view["pass_problem_sources"] == [kirkus]
    output = _completed_output(tmp_path, [_failed(kirkus), *_night(0, 5)], view)

    report = _diagnose(tmp_path, output)

    assert report.diagnosis is DiagnosisClass.SOURCE_FAILURE and exit_code_for(report) == EXIT_RECOVERABLE
    assert "Сбои отдельного прохода сбора (Kirkus, PM, PW): " + kirkus in report.verdict
    assert run_health(output, "2026-11-16") == 0
    assert "recurring_attention_required=true" in capsys.readouterr().out.splitlines()


def test_a_recovery_is_reported_again_by_a_second_run_on_its_date_and_never_later():
    nights = [(f"2026-11-1{day}", [_failed()]) for day in range(4)]

    _records, resumed = _runs(*nights, ("2026-11-15", [_collected()]), ("2026-11-15", [_collected()]))
    _records, later = _runs(
        *nights, ("2026-11-15", [_collected()]), ("2026-11-15", [_collected()]), ("2026-11-16", [_collected()])
    )

    assert resumed["recovered"] == [{"source_name": SOURCE, "failed_attempts": 4, "last_failure_date": "2026-11-13"}]
    assert later["recovered"] == []


@pytest.mark.parametrize("recovered", [None, 3, "x", [None, 5]])
def test_a_malformed_view_never_crashes_the_doctor_or_run_health(tmp_path, capsys, recovered):
    view = {**_view(_entry()), "recovered": recovered, "pass_problem_sources": None}
    output = _completed_output(tmp_path, [_failed()], view)

    assert _diagnose(tmp_path, output).diagnosis is DiagnosisClass.HEALTHY
    assert run_health(output, "2026-11-16") == 0


def test_a_rerun_after_feedback_keeps_the_view_of_the_last_collection(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path, entries=False)
    state_dir = tmp_path / "state"
    run_weekly_workflow(config, output_dir, state_dir, adapters=adapters)
    view = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))[RUN_SUMMARY_KEY]

    from autoscout.cli.weekly import run_mvp

    run_mvp(config, output_dir, state_dir)

    assert json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))[RUN_SUMMARY_KEY] == view


def test_a_source_that_failed_at_capture_is_counted(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path, entries=True)
    state_dir = tmp_path / "state"
    blocked = {
        "source_name": "Verso — Forthcoming",
        "status": "skipped",
        "reason": "Codex extraction skipped: source or backend reported captcha",
        "debug_metadata": {"safe_failure_status": "source_blocked"},
    }
    seed_state(state_dir, source_failure_streaks=[_streak("Verso — Forthcoming", 2, "1999-12-30")])
    original = adapters.capture.prepare

    def prepare_with_a_blocked_source(*args, **kwargs):
        result = original(*args, **kwargs)
        result["source_runs"] = [*result.get("source_runs", []), blocked]
        return result

    adapters.capture.prepare = prepare_with_a_blocked_source

    result = run_weekly_workflow(config, output_dir, state_dir, adapters=adapters)

    stored = {record["source_name"]: record for record in read_state(state_dir, Collection.SOURCE_FAILURE_STREAKS)}
    assert stored["Verso — Forthcoming"]["consecutive_failures"] == 3
    assert "sources" in result.attention_reasons


# ── Review round 2 ─────────────────────────────────────────────────────────────


class _Run:
    """The part of a weekly run's context the attempt merge reads."""

    def __init__(self, output_dir: Path, problems: list[dict[str, str]], passes: set[str]) -> None:
        self.output_dir = output_dir
        self.capture_timing = {"source_runs": []}
        self.operational_source_problems = problems
        self.pass_problem_names = passes


def test_an_extraction_failure_served_from_an_earlier_artifact_is_a_failed_attempt(tmp_path):
    from autoscout.weekly_workflow import _attempted_source_runs

    beacon = "Beacon Press — Forthcoming"
    reused = {"source_name": beacon, "status": "reused", "reason": "served from an earlier operation"}
    run = _Run(tmp_path, [{"source_name": beacon, "status": "failed", "reason": "invalid_output"}], set())

    attempts = _attempted_source_runs(run, [reused, _collected()], "")
    records, view = _runs(*[(f"2026-11-1{day}", attempts) for day in (4, 5, 6)])

    assert records[beacon]["consecutive_failures"] == 3 and records[beacon]["last_reason_code"] == "invalid_output"
    assert view["failing"][0]["broken"]


def test_a_failed_pass_counts_against_its_source_even_when_the_previous_result_was_used(tmp_path):
    from autoscout.weekly_workflow import _attempted_source_runs

    pm = "Publishers Marketplace Deals"
    problem = {"source_name": pm, "status": "failed", "reason": "PM deals pass failed (pm_login_failed); ..."}
    run = _Run(tmp_path, [problem], {pm})

    attempts = _attempted_source_runs(run, [_collected(pm)], "")
    records, view = _runs(("2026-11-16", attempts))

    assert records[pm]["consecutive_failures"] == 1 and records[pm]["last_reason_code"] == "source_pass_failed"
    assert BOOK_TITLE not in json.dumps(view) and "pm_login_failed" not in json.dumps(records)


def test_run_health_counts_capture_failures_from_the_view(tmp_path):
    view = {**_view(_entry("Independent Press Top 40 Nonfiction", 3)), "widespread": False}

    readiness = recurring_readiness(
        Path("unused"), True, 10, 0, 3, source_runs=[_collected()], source_failure_streaks=view
    )

    assert readiness["attention_required"] and readiness["health_classification"] == "degraded"


def test_a_pass_problem_of_a_source_served_from_its_previous_result_asks_for_attention_everywhere(tmp_path, capsys):
    pm = "Publishers Marketplace Deals"
    view = update_streaks([], [_collected(pm), *_night(0, 3)], "2026-11-16", 3, {pm}).view
    output = _completed_output(tmp_path, [_collected(pm), *_night(0, 3)], view)

    report = _diagnose(tmp_path, output)

    assert report.diagnosis is DiagnosisClass.SOURCE_FAILURE and exit_code_for(report) == EXIT_RECOVERABLE
    assert report.verdict.startswith("Поиск завершён, но отдельный проход сбора дал сбой. ")
    assert f"Сбои отдельного прохода сбора (Kirkus, PM, PW): {pm}." in report.verdict
    assert run_health(output, "2026-11-16") == 0
    assert "recurring_attention_required=true" in capsys.readouterr().out.splitlines()


def test_a_malformed_earlier_run_summary_does_not_stop_the_pipeline(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path, entries=True)
    (output_dir / "run-summary.json").write_text("{not json", encoding="utf-8")

    result = run_weekly_workflow(config, output_dir, tmp_path / "state", adapters=adapters)

    assert result.outcome.value.startswith("completed/"), result.failed_stage


# ── Review round 3 ─────────────────────────────────────────────────────────────


def test_the_replay_record_wins_when_the_ledgers_name_a_source_by_different_urls():
    capture = {**_collected(), "url": "https://example.test/old", "status": "codex-captured"}
    replay = {**_failed(code="page_shape"), "url": "https://example.test/new"}

    records, view = _runs(("2026-11-16", [capture, replay]))

    assert records[SOURCE]["consecutive_failures"] == 1
    assert [entry["source_name"] for entry in view["failing"]] == [SOURCE]


def test_a_stale_pass_collection_asks_for_attention_everywhere_on_its_first_failure(tmp_path, capsys):
    pw = "Publishers Weekly Nonfiction Reviews"
    stale = _failed(pw, "pw_stale_collection")
    view = update_streaks([], [stale, *_night(0, 5)], "2026-11-16").view
    problems = [{"source_name": pw, "status": "failed", "reason": "x"}]

    assert view["pass_problem_sources"] == [pw] and not view["failing"][0]["broken"]
    assert attention_source_problems(problems, view) == problems
    output = _completed_output(tmp_path, [stale, *_night(0, 5)], view)
    report = _diagnose(tmp_path, output)
    assert report.diagnosis is DiagnosisClass.SOURCE_FAILURE and exit_code_for(report) == EXIT_RECOVERABLE
    assert run_health(output, "2026-11-16") == 0
    assert "recurring_attention_required=true" in capsys.readouterr().out.splitlines()
    result = _result(WorkflowOutcome.COMPLETED_ATTENTION_REQUIRED, view, problems, attention_reasons=("sources",))
    body = run_report(result, run_date="2026-11-16", kirkus_attempt=None).body
    assert f"Сбои отдельного прохода сбора (Kirkus, PM, PW): {pw} (" in body
    assert "пока не требующие внимания" not in body


def test_a_problem_code_of_an_unexpected_type_does_not_stop_the_count():
    odd = {"source_name": SOURCE, "status": "skipped", "debug_metadata": {"safe_failure_status": ["captcha"]}}

    records, view = _runs(("2026-11-16", [odd]))

    assert records[SOURCE]["consecutive_failures"] == 1 and view["failing"][0]["reason_code"] == "source_skipped"
