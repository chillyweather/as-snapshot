"""The weekly run starts the PW issue pass; heartbeat, doctor and the pass lock (Issue #956).

Every issue here is invented: the mailbox, the subscriber session and the PDF
text are fakes from tests/test_pw_issue.py, so no test reaches PW.
"""

from __future__ import annotations

import json
import urllib.error
from datetime import UTC
from datetime import date
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from autoscout.doctor import DiagnosisClass
from autoscout.doctor import DoctorSeams
from autoscout.doctor import diagnose
from autoscout.doctor import render_russian
from autoscout.pw_issue_fetch import PwError
from autoscout.pw_precollection import REASON_HISTORICAL_RUN
from autoscout.pw_precollection import REASON_IN_PROGRESS
from autoscout.pw_precollection import STOP_LINK_REJECTED
from autoscout.pw_precollection import STOP_TIME_BUDGET
from autoscout.pw_precollection import _pass_lock
from autoscout.pw_precollection import last_pass_status
from autoscout.pw_precollection import run_pass
from autoscout.pw_precollection import weekly_source_problem
from autoscout.pw_precollection import weekly_stage
from autoscout.russian_workflow import snapshot_progress_line
from autoscout.russian_workflow import source_problem_message
from tests.test_issue_706_description_progress import _live_guard
from tests.test_pw_issue import PAGE
from tests.test_pw_issue import TOKEN
from tests.test_pw_issue import TRACKER
from tests.test_pw_issue import IssueClient
from tests.test_pw_issue import Mailbox
from tests.test_pw_issue import issue_header
from tests.test_pw_issue import numbered_pages

NOW = datetime(2026, 11, 4, 6, 0, tzinfo=UTC)
SOURCE_NAME = "Publishers Weekly Nonfiction Reviews"
CREDENTIALS = {
    "AUTOSCOUT_PM_IMAP_USER": "fixture",
    "AUTOSCOUT_PM_IMAP_PASSWORD": TOKEN,
    "AUTOSCOUT_PW_FORWARDING_SENDER": "editor@example.com",
}
BODY = (
    "From: editor@example.com\nContent-Type: text/plain; charset=utf-8\n\n"
    f"From: Publishers Weekly <delivery@mail.emagazines.com>\n{TRACKER}"
).encode()
NOV_2 = issue_header(4, "November 2, 2026", datetime(2026, 11, 3, tzinfo=UTC))
OCT_26 = issue_header(3, "October 26, 2026", datetime(2026, 10, 27, tzinfo=UTC))
OCT_19 = issue_header(2, "October 19, 2026", datetime(2026, 10, 20, tzinfo=UTC))


def _weekly_config(tmp_path: Path, **overrides: Any) -> Path:
    """A weekly config with the PW declaration beside it, as in configs/."""
    weekly = tmp_path / "weekly.json"
    if not weekly.exists():
        weekly.write_text("{}", encoding="utf-8")
    declared = json.loads(Path("configs/pw-issue-pdf.json").read_text(encoding="utf-8"))
    declared.update(overrides)
    declared["collection_file"] = "data/pw/collection.json"
    declared["attempt_file"] = "data/pw/attempt.json"
    (tmp_path / "pw-issue-pdf.json").write_text(json.dumps(declared), encoding="utf-8")
    return weekly


def _data(tmp_path: Path, name: str) -> Path:
    return tmp_path / "data" / "pw" / name


class Clock:
    def __init__(self, step: float = 0.0) -> None:
        self.now, self.step = 0.0, step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def _stage(tmp_path: Path, headers: list[Any], client: Any, **kwargs: Any) -> dict[str, Any]:
    output_dir = tmp_path / "run"
    output_dir.mkdir(exist_ok=True)
    kwargs.setdefault("text_reader", numbered_pages())
    kwargs.setdefault("now", NOW)
    return weekly_stage(
        _weekly_config(tmp_path, **kwargs.pop("declaration", {})),
        output_dir,
        opener=lambda *_args: Mailbox(list(headers), BODY),
        client=client,
        environ=CREDENTIALS,
        key_file=tmp_path / "missing.env",
        **kwargs,
    )


def _refused(code: int = 403) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.invalid", code, "Forbidden", None, None)  # type: ignore[arg-type]


# ── Outcomes of the pass ──────────────────────────────────────────────────


def test_a_new_issue_is_collected_and_named_as_such(tmp_path: Path) -> None:
    report = _stage(tmp_path, [NOV_2], IssueClient(["20261102"]))

    assert report["status"] == "collected"
    assert report["outcome"] == "new_issue"
    assert report["issues_collected"] == ["2026-11-02"]
    assert weekly_source_problem(report, SOURCE_NAME) is None


def test_a_week_with_no_new_issue_opens_no_subscriber_link(tmp_path: Path) -> None:
    _stage(tmp_path, [NOV_2], IssueClient(["20261102"]))
    client = IssueClient([])

    report = _stage(tmp_path, [NOV_2], client)

    assert (report["status"], report["outcome"]) == ("no_new_issue", "no_new_issue")
    # No subscriber link was opened, so no PW device session was used.
    assert client.calls == []
    assert weekly_source_problem(report, SOURCE_NAME) is None


def test_a_refused_link_stops_the_pass_and_tells_the_editor_what_to_do(tmp_path: Path) -> None:
    client = IssueClient(["20261019", "20261026"], fail={"20261019": _refused()})

    report = _stage(tmp_path, [OCT_19, OCT_26], client)

    # One refused link, and no further attempt: each would open another device session.
    assert len(client.calls) == 1
    assert report == {**report, "status": "not_collected", "reason_code": STOP_LINK_REJECTED}
    problem = weekly_source_problem(report, SOURCE_NAME)
    assert problem is not None and problem["status"] == "failed"
    message = source_problem_message([problem])
    assert SOURCE_NAME in message and "подтвердите аккаунт" in message
    # The refused issue waits for a retry; the other one was never attempted.
    result = json.loads(_data(tmp_path, "collection.json").read_text(encoding="utf-8"))
    assert (result["retry_issues"], result["untried_issues"]) == (["2026-10-19"], ["2026-10-26"])
    attempt = json.loads(_data(tmp_path, "attempt.json").read_text(encoding="utf-8"))
    assert attempt["issues_pending"] == 1


def test_a_refusal_after_a_collected_issue_is_a_named_partial_pass(tmp_path: Path) -> None:
    client = IssueClient(["20261019", "20261026", "20261102"], fail={"20261026": _refused(410)})

    report = _stage(tmp_path, [OCT_19, OCT_26, NOV_2], client)

    assert report["status"] == "partial"
    assert report["stop_reason"] == STOP_LINK_REJECTED
    assert report["issues_collected"] == ["2026-10-19"]
    assert report["issues_pending"] == 1
    problem = weekly_source_problem(report, SOURCE_NAME)
    assert problem is not None and problem["status"] == "incomplete"
    assert "подтвердите аккаунт" in source_problem_message([problem])


def test_an_issue_with_no_nonfiction_review_is_a_named_failure(tmp_path: Path) -> None:
    report = _stage(tmp_path, [NOV_2], IssueClient(["20261102"]), text_reader=lambda _path: ["Fiction Reviews"])

    assert report["status"] == "not_collected"
    assert report["reason_code"] == "pw_zero_nonfiction_rows"
    assert weekly_source_problem(report, SOURCE_NAME)["status"] == "failed"  # type: ignore[index]


def test_a_mailbox_failure_is_named_for_pw_and_never_raises(tmp_path: Path) -> None:
    def refuse(*_args: Any) -> Any:
        raise TimeoutError

    output_dir = tmp_path / "run"
    output_dir.mkdir()
    report = weekly_stage(_weekly_config(tmp_path), output_dir, opener=refuse, environ=CREDENTIALS, now=NOW)

    assert report["status"] == "not_collected"
    assert report["reason_code"] == "pw_collection_failed"


def test_an_unexpected_error_is_named_and_recorded_as_the_last_attempt(tmp_path: Path) -> None:
    class Broken(Mailbox):
        def headers_since(self, since: Any, senders: Any) -> Any:
            raise ZeroDivisionError

    output_dir = tmp_path / "run"
    output_dir.mkdir()
    report = weekly_stage(
        _weekly_config(tmp_path),
        output_dir,
        opener=lambda *_args: Broken([], BODY),
        environ=CREDENTIALS,
        now=NOW,
    )

    assert (report["status"], report["reason_code"]) == ("not_collected", "unexpected_error")
    attempt = json.loads(_data(tmp_path, "attempt.json").read_text(encoding="utf-8"))
    assert (attempt["status"], attempt["reason_code"]) == ("failed", "unexpected_error")


def test_a_weekly_config_without_pw_is_skipped(tmp_path: Path) -> None:
    weekly = tmp_path / "weekly.json"
    weekly.write_text("{}", encoding="utf-8")

    assert weekly_stage(weekly, tmp_path) == {"status": "not_configured"}
    assert last_pass_status(weekly) is None


def test_a_weekly_run_for_an_earlier_date_reads_no_mail(tmp_path: Path) -> None:
    client = IssueClient(["20261102"])

    report = _stage(tmp_path, [NOV_2], client, run_date=date(2026, 10, 1))

    assert report == {"status": "not_run", "reason_code": REASON_HISTORICAL_RUN}
    assert client.calls == []


# ── Budget and lock ───────────────────────────────────────────────────────


def test_the_pass_starts_no_issue_past_its_budget_and_keeps_what_it_read(tmp_path: Path) -> None:
    client = IssueClient(["20261019", "20261026"])
    clock = Clock()
    pages = numbered_pages()

    def slow_reader(path: Path) -> list[str]:
        # Reading the first issue spends the whole budget, so the second never starts.
        clock.now += 300
        return pages(path)

    report = _stage(tmp_path, [OCT_19, OCT_26], client, monotonic=clock, budget_seconds=250, text_reader=slow_reader)

    assert report["status"] == "partial"
    assert report["stop_reason"] == STOP_TIME_BUDGET
    assert report["issues_collected"] == ["2026-10-19"]
    assert report["issues_pending"] == 1
    assert weekly_source_problem(report, SOURCE_NAME)["status"] == "incomplete"  # type: ignore[index]
    # The next pass collects the issue left pending.
    assert _stage(tmp_path, [OCT_19, OCT_26], IssueClient(["20261026"]))["issues_collected"] == ["2026-10-26"]


def test_a_second_pass_while_one_runs_is_refused_by_name_and_writes_nothing(tmp_path: Path) -> None:
    from autoscout.pm_mailbox import load_source

    config = _weekly_config(tmp_path).parent / "pw-issue-pdf.json"
    source = load_source(config, family="pw-issue-pdf")

    with _pass_lock(source):
        with pytest.raises(PwError, match=f"^{REASON_IN_PROGRESS}$"):
            run_pass(config, now=NOW, opener=lambda *_args: Mailbox([NOV_2], BODY), environ=CREDENTIALS)
        report = _stage(tmp_path, [NOV_2], IssueClient(["20261102"]))

    assert report == {**report, "status": "not_collected", "reason_code": REASON_IN_PROGRESS}
    assert not _data(tmp_path, "collection.json").exists()
    assert not _data(tmp_path, "attempt.json").exists()


# ── Heartbeat ─────────────────────────────────────────────────────────────


def test_heartbeat_rows_count_issues_and_carry_no_title(tmp_path: Path) -> None:
    _stage(tmp_path, [OCT_26, NOV_2], IssueClient(["20261026", "20261102"]))

    snapshot = json.loads((tmp_path / "run" / "progress-snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["stage"] == "pw-precollection"
    assert snapshot_progress_line(snapshot) == "Сбор PW: обработано выпусков 2."
    text = json.dumps(snapshot)
    assert "An Invented Coast" not in text and "Digital Edition" not in text and TOKEN not in text


# ── The weekly run ────────────────────────────────────────────────────────


def _prepared_run(tmp_path: Path, *, with_pw_config: bool = True) -> tuple[Path, Path]:
    from autoscout.weekly_workflow import deterministic_adapters
    from tests.test_weekly_workflow import _write_capture_fixture
    from tests.test_weekly_workflow import _write_codex_fixture_config
    from tests.test_weekly_workflow import _write_extraction_fixture

    config_path = _write_codex_fixture_config(tmp_path)
    if with_pw_config:
        _weekly_config(tmp_path)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    _write_capture_fixture(tmp_path, output_dir)
    deterministic_adapters().capture.prepare(config_path, output_dir, live=False)
    _write_extraction_fixture(output_dir)
    return config_path, output_dir


class FakePwStage:
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


def _weekly_run(tmp_path: Path, pw: Any, *, with_pw_config: bool = True) -> tuple[Any, Path]:
    from autoscout.weekly_workflow import deterministic_adapters
    from autoscout.weekly_workflow import run_weekly_workflow

    config_path, output_dir = _prepared_run(tmp_path, with_pw_config=with_pw_config)
    adapters = deterministic_adapters()
    adapters.pw_precollection = pw
    return run_weekly_workflow(config_path, output_dir, adapters=adapters), output_dir


def _pw_entries(output_dir: Path) -> list[dict[str, Any]]:
    log = json.loads((output_dir / "workflow-log.json").read_text(encoding="utf-8"))
    entries = log if isinstance(log, list) else log.get("entries", [])
    return [entry for entry in entries if entry.get("stage") == "pw-precollection"]


@pytest.mark.parametrize(
    "pw",
    [
        FakePwStage({"status": "not_collected", "reason_code": STOP_LINK_REJECTED}),
        FakePwStage(error=RuntimeError("adapter broke")),
    ],
    ids=["named-failure", "adapter-raises"],
)
def test_a_failed_pw_pass_leaves_the_run_intact_with_pw_named(tmp_path: Path, pw: FakePwStage) -> None:
    result, output_dir = _weekly_run(tmp_path, pw)

    assert result.outcome.value != "intervention-required"
    assert len(pw.calls) == 1
    assert pw.calls[0]["budget_seconds"] <= 900
    names = [problem.get("source_name") for problem in result.source_problems if problem.get("status") == "failed"]
    assert SOURCE_NAME in names
    entries = _pw_entries(output_dir)
    assert [entry["status"] for entry in entries] == ["ok"]
    assert entries[0]["detail"]["status"] == "not_collected"


def test_a_quiet_pw_week_is_logged_and_names_no_problem(tmp_path: Path) -> None:
    pw = FakePwStage({"status": "no_new_issue", "outcome": "no_new_issue", "issues_collected": []})

    result, output_dir = _weekly_run(tmp_path, pw)

    assert SOURCE_NAME not in [problem.get("source_name") for problem in result.source_problems]
    assert _pw_entries(output_dir)[0]["detail"]["outcome"] == "no_new_issue"


def test_the_run_starts_no_pw_pass_without_a_pw_declaration(tmp_path: Path) -> None:
    pw = FakePwStage({"status": "no_new_issue"})

    _, output_dir = _weekly_run(tmp_path, pw, with_pw_config=False)

    assert pw.calls == []
    assert _pw_entries(output_dir) == []


def test_a_resumed_run_starts_no_second_pass_and_still_names_pw(tmp_path: Path) -> None:
    from autoscout.weekly_workflow import deterministic_adapters
    from autoscout.weekly_workflow import run_weekly_workflow

    class FailingCodex:
        def fulfill(self, output_dir: Path, **_: Any) -> dict[str, Any]:
            return {"status": "failed", "reason": "controlled_test_failure", "duration_seconds": 0.0}

    pw = FakePwStage({"status": "not_collected", "reason_code": STOP_LINK_REJECTED})
    interrupted = deterministic_adapters()
    interrupted.pw_precollection = pw
    interrupted.codex = FailingCodex()
    config_path, output_dir = _prepared_run(tmp_path)
    first = run_weekly_workflow(config_path, output_dir, adapters=interrupted)
    assert first.outcome.value == "intervention-required"

    adapters = deterministic_adapters()
    adapters.pw_precollection = pw
    resumed = run_weekly_workflow(config_path, output_dir, adapters=adapters)

    assert resumed.outcome.value.startswith("completed/"), resumed.failed_stage
    assert len(pw.calls) == 1
    assert SOURCE_NAME in [problem.get("source_name") for problem in resumed.source_problems]


# ── Doctor ────────────────────────────────────────────────────────────────


def _doctor(tmp_path: Path, output_dir: Path, *, live: bool = False) -> Any:
    seams = DoctorSeams(
        today=lambda: NOW.date(),
        repo_inspect=lambda _allowlist: {"status": "ok", "branch": "main"},
        codex_probe=lambda: {"codex_available": True, "codex_version": "0.1.0"},
        guard_status=(lambda: _live_guard(12.0)) if live else None,
    )
    return diagnose(tmp_path / "weekly.json", output_dir, seams=seams)


def test_doctor_reports_the_last_pw_pass_when_idle(tmp_path: Path) -> None:
    _stage(tmp_path, [NOV_2], IssueClient(["20261102"]), text_reader=lambda _path: [PAGE])

    report = _doctor(tmp_path, tmp_path / "run")

    pw = report.facts["pw_issue"]
    assert pw["attempt"]["status"] == "collected"
    assert pw["result"] == {
        "newest_issue_date": "2026-11-02",
        "issues_in_result": 1,
        "rows_in_result": 1,
        "retry_issues": 0,
    }
    russian = render_russian(report)
    assert "Сбор PW 2026-11-04: выпусков собрано 1, рецензий 1." in russian
    assert "В результате выпусков: 1, рецензий: 1 (последний выпуск 2026-11-02)." in russian
    assert "An Invented Coast" not in russian


def test_doctor_names_a_refused_link_and_the_issues_waiting(tmp_path: Path) -> None:
    _stage(tmp_path, [OCT_19], IssueClient(["20261019"], fail={"20261019": _refused()}))

    russian = render_russian(_doctor(tmp_path, tmp_path / "run"))

    assert "Сбор PW 2026-11-04: не удался — подписка не открыла выпуск" in russian
    assert "Ждут повтора: 1." in russian


def test_doctor_reports_the_pw_stage_of_a_live_run_without_a_question(tmp_path: Path) -> None:
    _weekly_config(tmp_path)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    (output_dir / "progress-snapshot.json").write_text(
        json.dumps(
            {
                "stage": "pw-precollection",
                "status": "running",
                "elapsed_seconds": 12.0,
                "stage_budget_seconds": 900,
                "rows": [{"source_name": "PW", "state": "finished"}, {"source_name": "PW", "state": "in_flight"}],
            }
        ),
        encoding="utf-8",
    )

    report = _doctor(tmp_path, output_dir, live=True)

    assert report.diagnosis is DiagnosisClass.RUN_IN_PROGRESS, report.verdict
    assert report.editor_question == ""
    assert report.facts["stage"] == "pw-precollection"
    assert "Сбор PW: обработано выпусков 1." in report.verdict
    assert "Сбор PW ещё не проводился." in render_russian(report)


# ── Review findings ───────────────────────────────────────────────────────


def test_a_refusal_after_another_failure_still_names_the_refusal(tmp_path: Path) -> None:
    down = urllib.error.URLError("down")
    client = IssueClient(["20261019", "20261026", "20261102"], fail={"20261019": down, "20261026": _refused()})

    report = _stage(tmp_path, [OCT_19, OCT_26, NOV_2], client)

    assert (report["status"], report["reason_code"]) == ("not_collected", STOP_LINK_REJECTED)
    assert "подтвердите аккаунт" in source_problem_message([weekly_source_problem(report, SOURCE_NAME)])  # type: ignore[list-item]


def test_a_refusal_past_the_budget_is_not_renamed_a_timeout(tmp_path: Path) -> None:
    clock = Clock()
    pages = numbered_pages()

    def slow_reader(path: Path) -> list[str]:
        clock.now += 300
        return pages(path)

    class SlowRefusal(IssueClient):
        def open(self, url: str) -> Any:
            if not url.endswith(".pdf") and self.stamps[:1] == ["20261026"]:
                # The refused request itself runs past the budget.
                clock.now += 200
            return super().open(url)

    client = SlowRefusal(["20261019", "20261026", "20261102"], fail={"20261026": _refused()})
    report = _stage(
        tmp_path,
        [OCT_19, OCT_26, NOV_2],
        client,
        monotonic=clock,
        budget_seconds=400,
        text_reader=slow_reader,
    )

    assert report["status"] == "partial"
    assert report["stop_reason"] == STOP_LINK_REJECTED


def test_a_damaged_pw_file_never_breaks_the_diagnosis(tmp_path: Path) -> None:
    _stage(tmp_path, [NOV_2], IssueClient(["20261102"]))
    _data(tmp_path, "attempt.json").write_text("{not json", encoding="utf-8")
    _data(tmp_path, "collection.json").write_text("{not json", encoding="utf-8")

    report = _doctor(tmp_path, tmp_path / "run")

    assert report.facts["pw_issue"]["unreadable"] == ["attempt.json", "collection.json"]
    assert "Файлы сбора PW повреждены: attempt.json, collection.json." in render_russian(report)


def test_issues_left_over_the_per_pass_cap_are_named_as_incomplete(tmp_path: Path) -> None:
    report = {"status": "collected", "issues_collected": ["2026-10-19"], "issues_pending": 2}

    problem = weekly_source_problem(report, SOURCE_NAME)

    assert problem is not None and problem["status"] == "incomplete"
    assert SOURCE_NAME in source_problem_message([problem])


def test_the_heartbeat_shows_the_pass_at_work_before_any_issue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    from autoscout import pw_precollection

    monkeypatch.setattr(pw_precollection, "PROGRESS_REFRESH_SECONDS", 0.05)
    seen: list[dict[str, Any]] = []

    def slow_login(*_args: Any) -> Any:
        time.sleep(0.3)
        seen.append(json.loads((tmp_path / "run" / "progress-snapshot.json").read_text(encoding="utf-8")))
        return Mailbox([], BODY)

    output_dir = tmp_path / "run"
    output_dir.mkdir()
    weekly_stage(_weekly_config(tmp_path), output_dir, opener=slow_login, environ=CREDENTIALS, now=NOW)

    assert seen and seen[0]["status"] == "running"
    assert [row["state"] for row in seen[0]["rows"]] == ["in_flight"]
    assert snapshot_progress_line(seen[0]) == "Сбор PW: обработано выпусков 0."


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("collection.json", {"schema_version": 1, "rows": [], "issues": [], "retry_issues": 1}),
        ("collection.json", {"schema_version": 1, "rows": [], "issues": [{"no_date": 1}]}),
        ("attempt.json", {"status": "partial", "issues_failed": ["not an object"]}),
    ],
)
def test_a_structurally_damaged_pw_file_never_breaks_the_diagnosis(
    tmp_path: Path, name: str, content: dict[str, Any]
) -> None:
    _stage(tmp_path, [NOV_2], IssueClient(["20261102"]))
    _data(tmp_path, name).write_text(json.dumps(content), encoding="utf-8")

    report = _doctor(tmp_path, tmp_path / "run")

    assert report.facts["pw_issue"]["unreadable"] == [name]
    assert f"Файлы сбора PW повреждены: {name}." in render_russian(report)


# ── Issues a pass leaves untried stay reachable ───────────────────────────

# Five weeks and more after the October emails: outside the 35-day mail window.
LATER = datetime(2026, 12, 9, 6, 0, tzinfo=UTC)


def _untried(tmp_path: Path) -> list[str]:
    return json.loads(_data(tmp_path, "collection.json").read_text(encoding="utf-8"))["untried_issues"]


def test_issues_untried_after_a_refusal_are_collected_once_their_emails_age_out(tmp_path: Path) -> None:
    _stage(tmp_path, [OCT_19, OCT_26], IssueClient(["20261019"], fail={"20261019": _refused()}))

    report = _stage(tmp_path, [OCT_19, OCT_26], IssueClient(["20261026", "20261019"]), now=LATER)

    # The never-attempted issue keeps its place ahead of the retry.
    assert report["issues_collected"] == ["2026-10-26", "2026-10-19"]
    assert _untried(tmp_path) == []


def test_issues_untried_past_the_budget_are_collected_once_their_emails_age_out(tmp_path: Path) -> None:
    clock = Clock()
    pages = numbered_pages()

    def slow_reader(path: Path) -> list[str]:
        clock.now += 300
        return pages(path)

    _stage(
        tmp_path,
        [OCT_19, OCT_26],
        IssueClient(["20261019"]),
        monotonic=clock,
        budget_seconds=250,
        text_reader=slow_reader,
    )
    assert _untried(tmp_path) == ["2026-10-26"]

    report = _stage(tmp_path, [OCT_19, OCT_26], IssueClient(["20261026"]), now=LATER)

    assert report["issues_collected"] == ["2026-10-26"]


def test_issues_over_the_per_pass_cap_are_collected_once_their_emails_age_out(tmp_path: Path) -> None:
    capped = {"max_issues_per_pass": 1}
    first = _stage(tmp_path, [OCT_19, OCT_26], IssueClient(["20261019"]), declaration=capped)
    assert (first["issues_collected"], first["issues_pending"]) == (["2026-10-19"], 1)
    assert _untried(tmp_path) == ["2026-10-26"]

    report = _stage(tmp_path, [OCT_19, OCT_26], IssueClient(["20261026"]), now=LATER, declaration=capped)

    assert report["issues_collected"] == ["2026-10-26"]
    assert _untried(tmp_path) == []
