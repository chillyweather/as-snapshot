"""The weekly run starts the PM deals pass; heartbeat, doctor and the pass lock (Issue #946)."""

from __future__ import annotations

import json
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from autoscout.doctor import DiagnosisClass
from autoscout.doctor import DoctorSeams
from autoscout.doctor import diagnose
from autoscout.doctor import render_russian
from autoscout.pm_mailbox import PmMailboxError
from autoscout.pm_mailbox import PmSource
from autoscout.pm_precollection import REASON_HISTORICAL_RUN
from autoscout.pm_precollection import REASON_IN_PROGRESS
from autoscout.pm_precollection import REASON_NO_DEALS_PARSED
from autoscout.pm_precollection import STOP_TIME_BUDGET
from autoscout.pm_precollection import last_pass_status
from autoscout.pm_precollection import run_pass
from autoscout.pm_precollection import weekly_source_problem
from autoscout.pm_precollection import weekly_stage
from autoscout.russian_workflow import snapshot_progress_line
from autoscout.russian_workflow import source_problem_message
from tests.pm_deal_fixtures import DAILY_DEALS_SUBJECT
from tests.pm_deal_fixtures import deal_text
from tests.pm_deal_fixtures import message
from tests.test_issue_706_description_progress import _live_guard
from tests.test_pm_precollection import CREDENTIALS
from tests.test_pm_precollection import NOW
from tests.test_pm_precollection import FakeMailbox

TITLE = "THE LANTERN ARCHIVE OF MISTWICK"
SOURCE_NAME = "Publishers Marketplace Deals"


def _weekly_config(tmp_path: Path) -> Path:
    """A weekly config with the PM declaration beside it, as in configs/."""
    weekly = tmp_path / "weekly.json"
    weekly.write_text("{}", encoding="utf-8")
    declared = {
        "name": SOURCE_NAME,
        "contract_family": "pm-deal-email",
        "mailbox": {
            "host": "imap.example.com",
            "port": 993,
            "folder": "PM",
            "user_env": "AUTOSCOUT_PM_IMAP_USER",
            "password_env": "AUTOSCOUT_PM_IMAP_PASSWORD",
        },
        "senders": ["daily@publisherslunch.com"],
        "subjects": {"daily_deals": DAILY_DEALS_SUBJECT},
        "max_message_age_days": 14,
        "max_messages_per_pass": 30,
        "collection_file": "data/pm/collection.json",
        "attempt_file": "data/pm/attempt.json",
        "watermark_file": "data/pm/watermark.json",
    }
    (tmp_path / "pm-deal-email.json").write_text(json.dumps(declared), encoding="utf-8")
    return weekly


def _data(tmp_path: Path, name: str) -> Path:
    return tmp_path / "data" / "pm" / name


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _stage(tmp_path: Path, mailbox: Any, **kwargs: Any) -> dict[str, Any]:
    output_dir = tmp_path / "run"
    output_dir.mkdir(exist_ok=True)
    return weekly_stage(
        _weekly_config(tmp_path),
        output_dir,
        opener=lambda source, user, password: mailbox,
        environ=CREDENTIALS,
        key_file=tmp_path / "missing.env",
        now=NOW,
        **kwargs,
    )


# ── Outcomes of the pass ──────────────────────────────────────────────────


def test_new_deals_are_collected_and_named_as_such(tmp_path: Path) -> None:
    mailbox = FakeMailbox()
    mailbox.add(1, message(), subject=DAILY_DEALS_SUBJECT)

    report = _stage(tmp_path, mailbox)

    assert report["status"] == "collected"
    assert report["outcome"] == "new_deals"
    assert report["deals_new"] == 5
    assert weekly_source_problem(report, SOURCE_NAME) is None


def test_emails_with_no_parsed_deal_are_a_named_failure_and_write_no_result(tmp_path: Path) -> None:
    mailbox = FakeMailbox()
    mailbox.add(
        1, message("Here are the most recent deals.\n\nNo deal in a shape we know.\n"), subject=DAILY_DEALS_SUBJECT
    )

    report = _stage(tmp_path, mailbox)

    assert report["status"] == "not_collected"
    assert report["reason_code"] == REASON_NO_DEALS_PARSED
    assert report["messages_read"] == 1
    attempt = json.loads(_data(tmp_path, "attempt.json").read_text(encoding="utf-8"))
    assert attempt["status"] == "not_collected"
    assert attempt["reason_code"] == REASON_NO_DEALS_PARSED
    assert attempt["counts"]["messages_read"] == 1
    # Neither the result nor the watermark moves, so the next pass reads the email again.
    assert not _data(tmp_path, "collection.json").exists()
    assert not _data(tmp_path, "watermark.json").exists()
    problem = weekly_source_problem(report, SOURCE_NAME)
    assert problem is not None and problem["status"] == "failed"
    assert SOURCE_NAME in source_problem_message([problem])


def test_a_quiet_week_is_not_a_failure(tmp_path: Path) -> None:
    """A repeated deal (or no new mail) is distinguishable from emails that parse into nothing."""
    mailbox = FakeMailbox()
    mailbox.add(1, message(deal_text(TITLE)), subject=DAILY_DEALS_SUBJECT)
    assert _stage(tmp_path, mailbox)["outcome"] == "new_deals"

    mailbox.add(2, message(deal_text(TITLE), message_id="<second@example.com>"), subject=DAILY_DEALS_SUBJECT)
    repeated = _stage(tmp_path, mailbox)
    assert repeated["status"] == "collected"
    assert repeated["outcome"] == "no_new_deals"
    assert repeated["deals_parsed"] == 1

    quiet = _stage(tmp_path, mailbox)
    assert quiet["status"] == "collected"
    assert quiet["outcome"] == "no_new_messages"
    assert weekly_source_problem(repeated, SOURCE_NAME) is None
    assert weekly_source_problem(quiet, SOURCE_NAME) is None


def test_a_login_failure_is_named_for_pm_and_never_raises(tmp_path: Path) -> None:
    def refuse(source: PmSource, user: str, password: str) -> Any:
        raise PmMailboxError("pm_login_failed", "the PM mail server refused the login")

    output_dir = tmp_path / "run"
    output_dir.mkdir()
    report = weekly_stage(
        _weekly_config(tmp_path), output_dir, opener=refuse, environ=CREDENTIALS, key_file=tmp_path / "missing.env"
    )

    assert report["status"] == "not_collected"
    assert report["reason_code"] == "pm_login_failed"
    assert CREDENTIALS["AUTOSCOUT_PM_IMAP_PASSWORD"] not in json.dumps(report)


def test_an_unexpected_error_is_a_named_failure(tmp_path: Path) -> None:
    class Broken(FakeMailbox):
        def headers_since(self, since: Any, senders: Any) -> Any:
            raise ZeroDivisionError

    report = _stage(tmp_path, Broken())

    assert report == {**report, "status": "not_collected", "reason_code": "unexpected_error"}


def test_a_weekly_config_without_pm_is_skipped(tmp_path: Path) -> None:
    weekly = tmp_path / "weekly.json"
    weekly.write_text("{}", encoding="utf-8")

    assert weekly_stage(weekly, tmp_path / "run") == {"status": "not_configured"}
    assert last_pass_status(weekly) is None


# ── The pass lock ─────────────────────────────────────────────────────────


def test_a_second_pass_while_one_runs_is_refused_by_name_and_writes_nothing(tmp_path: Path) -> None:
    config = tmp_path / "pm-deal-email.json"
    _weekly_config(tmp_path)
    inner: list[Any] = []

    class Overlapping(FakeMailbox):
        def fetch(self, uid: int) -> bytes:
            # A second pass starts while the first one is inside the mailbox.
            try:
                run_pass(config, now=NOW, opener=lambda *_: FakeMailbox(), environ=CREDENTIALS)
            except PmMailboxError as exc:
                inner.append(exc.reason_code)
                inner.append(json.loads(_data(tmp_path, "attempt.json").read_text(encoding="utf-8"))["status"])
            return super().fetch(uid)

    mailbox = Overlapping()
    mailbox.add(1, message(), subject=DAILY_DEALS_SUBJECT)
    summary = run_pass(config, now=NOW, opener=lambda *_: mailbox, environ=CREDENTIALS)

    # The refused pass did not overwrite the running pass's attempt record.
    assert inner == [REASON_IN_PROGRESS, "started"]
    assert summary["deals_new"] == 5
    # Once the first pass ends, the lock is free again.
    assert run_pass(config, now=NOW, opener=lambda *_: FakeMailbox(), environ=CREDENTIALS)["status"] == "collected"


def test_the_weekly_stage_names_a_pass_already_in_progress(tmp_path: Path) -> None:
    import fcntl

    _weekly_config(tmp_path)
    lock = _data(tmp_path, "attempt.lock")
    lock.parent.mkdir(parents=True)
    with lock.open("a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        report = _stage(tmp_path, FakeMailbox())

    assert report["status"] == "not_collected"
    assert report["reason_code"] == REASON_IN_PROGRESS
    problem = weekly_source_problem(report, SOURCE_NAME)
    assert problem is not None and REASON_IN_PROGRESS in problem["reason"]


# ── Time budget ───────────────────────────────────────────────────────────


def test_the_pass_fetches_no_message_past_its_budget_and_keeps_what_it_read(tmp_path: Path) -> None:
    clock = Clock()

    class Slow(FakeMailbox):
        def fetch(self, uid: int) -> bytes:
            clock.now += 40.0
            return super().fetch(uid)

    mailbox = Slow()
    for uid in (1, 2, 3):
        mailbox.add(
            uid,
            message(deal_text(f"BOOK NUMBER {uid}"), message_id=f"<m{uid}@example.com>"),
            subject=DAILY_DEALS_SUBJECT,
        )

    report = _stage(tmp_path, mailbox, monotonic=clock, budget_seconds=60.0)

    assert report["status"] == "partial"
    assert report["stop_reason"] == STOP_TIME_BUDGET
    assert mailbox.fetched == [1, 2]
    watermark = json.loads(_data(tmp_path, "watermark.json").read_text(encoding="utf-8"))
    assert watermark["last_uid"] == 2
    problem = weekly_source_problem(report, SOURCE_NAME)
    assert problem is not None and problem["status"] == "incomplete"
    assert SOURCE_NAME in source_problem_message([problem])


def test_the_stage_stops_waiting_for_a_hung_mail_server(tmp_path: Path) -> None:
    release = threading.Event()

    class Hung(FakeMailbox):
        def fetch(self, uid: int) -> bytes:
            release.wait(10)
            return super().fetch(uid)

    mailbox = Hung()
    mailbox.add(1, message(), subject=DAILY_DEALS_SUBJECT)
    try:
        report = _stage(tmp_path, mailbox, budget_seconds=0.2)
        snapshot = json.loads((tmp_path / "run" / "progress-snapshot.json").read_text(encoding="utf-8"))
    finally:
        release.set()

    assert report["status"] == "not_collected"
    assert report["reason_code"] == STOP_TIME_BUDGET
    assert snapshot["stage"] == "pm-precollection"
    assert snapshot["status"] == "finished"


def test_a_pass_the_stage_stopped_waiting_for_writes_no_result_later(tmp_path: Path) -> None:
    release = threading.Event()

    class Hung(FakeMailbox):
        def fetch(self, uid: int) -> bytes:
            release.wait(10)
            return super().fetch(uid)

    mailbox = Hung()
    mailbox.add(1, message(), subject=DAILY_DEALS_SUBJECT)
    report = _stage(tmp_path, mailbox, budget_seconds=0.2)
    assert report["reason_code"] == STOP_TIME_BUDGET

    release.set()
    for _ in range(100):
        attempt = json.loads(_data(tmp_path, "attempt.json").read_text(encoding="utf-8"))
        if attempt["status"] != "started":
            break
        time.sleep(0.05)

    # The late pass ends as the stage reported it, and capture never sees a result appear behind it.
    assert (attempt["status"], attempt["reason_code"]) == ("not_collected", STOP_TIME_BUDGET)
    assert not _data(tmp_path, "collection.json").exists()
    assert not _data(tmp_path, "watermark.json").exists()


def test_a_slow_mailbox_keeps_the_emails_read_before_the_cut_off(tmp_path: Path) -> None:
    class Slow(FakeMailbox):
        def fetch(self, uid: int) -> bytes:
            time.sleep(0.3)
            return super().fetch(uid)

    mailbox = Slow()
    for uid in range(1, 11):
        mailbox.add(uid, message(message_id=f"<{uid}@x>"), subject=DAILY_DEALS_SUBJECT)

    # Real time: the pass stops fetching with a quarter of its budget left to write what it read.
    report = _stage(tmp_path, mailbox, budget_seconds=2.0)

    assert (report["status"], report["stop_reason"]) == ("partial", STOP_TIME_BUDGET)
    assert 1 <= report["messages_read"] < 10
    assert json.loads(_data(tmp_path, "collection.json").read_text(encoding="utf-8"))["rows"]
    watermark = json.loads(_data(tmp_path, "watermark.json").read_text(encoding="utf-8"))
    assert watermark["last_uid"] == report["messages_read"]


def test_a_weekly_run_for_an_earlier_date_reads_no_mail(tmp_path: Path) -> None:
    mailbox = FakeMailbox()
    mailbox.add(1, message(), subject=DAILY_DEALS_SUBJECT)

    report = _stage(tmp_path, mailbox, run_date=NOW.date() - timedelta(days=7))

    assert report == {"status": "not_run", "reason_code": REASON_HISTORICAL_RUN}
    assert mailbox.fetched == []
    assert not _data(tmp_path, "collection.json").exists()
    # Yesterday's date is still today's run in another time zone.
    assert _stage(tmp_path, mailbox, run_date=NOW.date() - timedelta(days=1))["status"] == "collected"


# ── Heartbeat ─────────────────────────────────────────────────────────────


def test_heartbeat_rows_count_emails_and_carry_no_title(tmp_path: Path) -> None:
    clock = Clock()
    presented: list[dict[str, Any]] = []

    class Ticking(FakeMailbox):
        def fetch(self, uid: int) -> bytes:
            clock.now += 61.0
            return super().fetch(uid)

    mailbox = Ticking()
    for uid in (1, 2):
        mailbox.add(
            uid, message(deal_text(f"{TITLE} {uid}"), message_id=f"<h{uid}@example.com>"), subject=DAILY_DEALS_SUBJECT
        )

    _stage(tmp_path, mailbox, monotonic=clock, presentation=presented.append, budget_seconds=1000.0)

    assert presented
    for snapshot in presented:
        assert snapshot["stage"] == "pm-precollection"
        assert {row["source_name"] for row in snapshot["rows"]} == {"PM"}
        assert TITLE not in json.dumps(snapshot)
    lines = [snapshot_progress_line(snapshot) for snapshot in presented]
    assert lines[0] == "Сбор PM: прочитано писем 0."
    assert "Сбор PM: прочитано писем 1." in lines
    assert TITLE not in (tmp_path / "run" / "progress-snapshot.json").read_text(encoding="utf-8")


# ── The weekly run ────────────────────────────────────────────────────────


def _prepared_run(tmp_path: Path, *, with_pm_config: bool = True) -> tuple[Path, Path]:
    from autoscout.weekly_workflow import deterministic_adapters
    from tests.test_weekly_workflow import _write_capture_fixture
    from tests.test_weekly_workflow import _write_codex_fixture_config
    from tests.test_weekly_workflow import _write_extraction_fixture

    config_path = _write_codex_fixture_config(tmp_path)
    if with_pm_config:
        _weekly_config(tmp_path)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    _write_capture_fixture(tmp_path, output_dir)
    deterministic_adapters().capture.prepare(config_path, output_dir, live=False)
    _write_extraction_fixture(output_dir)
    return config_path, output_dir


def _weekly_run(tmp_path: Path, pm: Any, *, with_pm_config: bool = True) -> tuple[Any, Path]:
    from autoscout.weekly_workflow import deterministic_adapters
    from autoscout.weekly_workflow import run_weekly_workflow

    config_path, output_dir = _prepared_run(tmp_path, with_pm_config=with_pm_config)
    adapters = deterministic_adapters()
    adapters.pm_precollection = pm
    return run_weekly_workflow(config_path, output_dir, adapters=adapters), output_dir


class FakePmStage:
    def __init__(self, result: dict[str, Any] | None = None, error: BaseException | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def collect(self, config_path: Path, output_dir: Path, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return dict(self.result)


def _pm_entries(output_dir: Path) -> list[dict[str, Any]]:
    log = json.loads((output_dir / "workflow-log.json").read_text(encoding="utf-8"))
    entries = log if isinstance(log, list) else log.get("entries", [])
    return [entry for entry in entries if entry.get("stage") == "pm-precollection"]


@pytest.mark.parametrize(
    "pm",
    [
        FakePmStage({"status": "not_collected", "reason_code": "pm_login_failed"}),
        FakePmStage(error=RuntimeError("adapter broke")),
    ],
    ids=["named-failure", "adapter-raises"],
)
def test_a_failed_pm_pass_leaves_the_run_intact_with_pm_named(tmp_path: Path, pm: FakePmStage) -> None:
    result, output_dir = _weekly_run(tmp_path, pm)

    assert result.outcome.value != "intervention-required"
    assert len(pm.calls) == 1
    assert pm.calls[0]["budget_seconds"] <= 300
    names = [problem.get("source_name") for problem in result.source_problems if problem.get("status") == "failed"]
    assert SOURCE_NAME in names
    entries = _pm_entries(output_dir)
    assert [entry["status"] for entry in entries] == ["ok"]
    assert entries[0]["detail"]["status"] == "not_collected"


def test_a_collected_pm_pass_is_logged_and_names_no_problem(tmp_path: Path) -> None:
    pm = FakePmStage({"status": "collected", "outcome": "no_new_deals", "messages_read": 3, "deals_new": 0})

    result, output_dir = _weekly_run(tmp_path, pm)

    assert SOURCE_NAME not in [problem.get("source_name") for problem in result.source_problems]
    assert _pm_entries(output_dir)[0]["detail"]["outcome"] == "no_new_deals"


def test_the_run_starts_no_pm_pass_without_a_pm_declaration(tmp_path: Path) -> None:
    pm = FakePmStage({"status": "collected"})

    _, output_dir = _weekly_run(tmp_path, pm, with_pm_config=False)

    assert pm.calls == []
    assert _pm_entries(output_dir) == []


# ── Doctor ────────────────────────────────────────────────────────────────


def _doctor(tmp_path: Path, output_dir: Path, *, live: bool = False) -> Any:
    seams = DoctorSeams(
        today=lambda: NOW.date(),
        repo_inspect=lambda _allowlist: {"status": "ok", "branch": "main"},
        codex_probe=lambda: {"codex_available": True, "codex_version": "0.1.0"},
        guard_status=(lambda: _live_guard(12.0)) if live else None,
    )
    return diagnose(tmp_path / "weekly.json", output_dir, seams=seams)


def test_doctor_reports_the_last_pm_pass_when_idle(tmp_path: Path) -> None:
    mailbox = FakeMailbox()
    mailbox.add(1, message(), subject=DAILY_DEALS_SUBJECT)
    _stage(tmp_path, mailbox)

    report = _doctor(tmp_path, tmp_path / "run")

    pm = report.facts["pm_deals"]
    assert pm["attempt"]["status"] == "collected"
    assert pm["attempt"]["attempted_at"] == NOW.isoformat()
    assert pm["attempt"]["counts"]["deals_new"] == 5
    assert pm["result"]["deals_in_result"] == 5
    russian = render_russian(report)
    assert "Сбор PM 2026-11-04: собран, писем прочитано 1, сделок разобрано 5, новых 5." in russian
    assert TITLE not in russian


def test_doctor_names_a_failed_pm_pass_and_the_result_it_keeps(tmp_path: Path) -> None:
    mailbox = FakeMailbox()
    mailbox.add(1, message(), subject=DAILY_DEALS_SUBJECT)
    _stage(tmp_path, mailbox)
    failing = FakeMailbox(fail_fetch=TimeoutError())
    failing.add(2, message(message_id="<late@example.com>"), subject=DAILY_DEALS_SUBJECT)
    _stage(tmp_path, failing)

    russian = render_russian(_doctor(tmp_path, tmp_path / "run"))

    assert "Сбор PM 2026-11-04: не удался — почтовый сервер не ответил вовремя." in russian
    assert "В последнем результате сделок: 5 (от 2026-11-04)." in russian


def test_doctor_reports_the_pm_stage_of_a_live_run_without_a_question(tmp_path: Path) -> None:
    _weekly_config(tmp_path)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    (output_dir / "progress-snapshot.json").write_text(
        json.dumps(
            {
                "stage": "pm-precollection",
                "status": "running",
                "elapsed_seconds": 12.0,
                "stage_budget_seconds": 300,
                "rows": [{"source_name": "PM", "state": "finished"}, {"source_name": "PM", "state": "in_flight"}],
            }
        ),
        encoding="utf-8",
    )

    report = _doctor(tmp_path, output_dir, live=True)

    assert report.diagnosis is DiagnosisClass.RUN_IN_PROGRESS, report.verdict
    assert report.editor_question == ""
    assert report.facts["stage"] == "pm-precollection"
    assert "Сбор PM: прочитано писем 1." in report.verdict
    assert "из 5 мин" in report.verdict
    assert "Сбор PM ещё не проводился." in render_russian(report)


def test_the_snapshot_stays_fresh_during_a_slow_mail_server_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from autoscout import pm_precollection

    monkeypatch.setattr(pm_precollection, "PROGRESS_REFRESH_SECONDS", 0.05)
    seen: list[float] = []

    def slow_login(source: PmSource, user: str, password: str) -> Any:
        time.sleep(0.4)
        snapshot = json.loads((tmp_path / "run" / "progress-snapshot.json").read_text(encoding="utf-8"))
        seen.append(snapshot["elapsed_seconds"])
        return FakeMailbox()

    output_dir = tmp_path / "run"
    output_dir.mkdir()
    report = weekly_stage(
        _weekly_config(tmp_path), output_dir, opener=slow_login, environ=CREDENTIALS, key_file=tmp_path / "x.env"
    )

    assert report["status"] == "collected"
    # Published while the login was still running, not only at the start.
    assert seen and seen[0] > 0.1


def test_a_resumed_run_starts_no_second_pass_and_still_names_pm(tmp_path: Path) -> None:
    from autoscout.weekly_workflow import deterministic_adapters
    from autoscout.weekly_workflow import run_weekly_workflow

    class FailingCodex:
        def fulfill(self, output_dir: Path, **_: Any) -> dict[str, Any]:
            return {"status": "failed", "reason": "controlled_test_failure", "duration_seconds": 0.0}

    pm = FakePmStage({"status": "not_collected", "reason_code": "pm_login_failed"})
    interrupted = deterministic_adapters()
    interrupted.pm_precollection = pm
    interrupted.codex = FailingCodex()
    config_path, output_dir = _prepared_run(tmp_path)
    first = run_weekly_workflow(config_path, output_dir, adapters=interrupted)
    assert first.outcome.value == "intervention-required"
    assert len(pm.calls) == 1

    adapters = deterministic_adapters()
    adapters.pm_precollection = pm
    resumed = run_weekly_workflow(config_path, output_dir, adapters=adapters)

    assert resumed.outcome.value.startswith("completed/"), resumed.failed_stage
    assert len(pm.calls) == 1
    assert SOURCE_NAME in [problem.get("source_name") for problem in resumed.source_problems]


def test_an_unexpected_error_is_recorded_as_the_last_attempt(tmp_path: Path) -> None:
    class Broken(FakeMailbox):
        def headers_since(self, since: Any, senders: Any) -> Any:
            raise ZeroDivisionError

    _stage(tmp_path, Broken())

    attempt = json.loads(_data(tmp_path, "attempt.json").read_text(encoding="utf-8"))
    assert attempt["status"] == "not_collected"
    assert attempt["reason_code"] == "unexpected_error"
    assert "непредвиденная ошибка" in render_russian(_doctor(tmp_path, tmp_path / "run"))


def test_the_heartbeat_speaks_during_a_slow_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    from autoscout import pm_precollection

    monkeypatch.setattr(pm_precollection, "PROGRESS_REFRESH_SECONDS", 0.05)
    presented: list[dict[str, Any]] = []

    def slow_login(source: PmSource, user: str, password: str) -> Any:
        time.sleep(0.2)
        return FakeMailbox()

    output_dir = tmp_path / "run"
    output_dir.mkdir()
    weekly_stage(
        _weekly_config(tmp_path),
        output_dir,
        opener=slow_login,
        environ=CREDENTIALS,
        key_file=tmp_path / "x.env",
        presentation=presented.append,
    )

    # An empty mailbox behind a slow login still gives the operator a PM line, counting no email.
    assert [snapshot_progress_line(snapshot) for snapshot in presented][:1] == ["Сбор PM: прочитано писем 0."]


def test_a_failed_fetch_is_not_counted_as_a_read_email(tmp_path: Path) -> None:
    mailbox = FakeMailbox(fail_fetch=TimeoutError())
    mailbox.add(1, message(), subject=DAILY_DEALS_SUBJECT)

    report = _stage(tmp_path, mailbox)

    assert report["reason_code"] == "pm_timeout"
    snapshot = json.loads((tmp_path / "run" / "progress-snapshot.json").read_text(encoding="utf-8"))
    assert [row for row in snapshot["rows"] if row["state"] == "finished"] == []
