"""An unexpected error leaves its traceback and its kind for the operator, and nothing for the editor (#1158)."""

from __future__ import annotations

import json
import types
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from autoscout import kirkus_precollection
from autoscout import weekly_workflow
from autoscout.doctor import DiagnosisClass
from autoscout.doctor import DoctorSeams
from autoscout.doctor import diagnose
from autoscout.doctor import exit_code_for
from autoscout.doctor import render_russian
from autoscout.russian_workflow import intervention_message
from autoscout.unexpected_error import UNEXPECTED_ERROR_STAGE
from autoscout.unexpected_error import exception_location
from autoscout.unexpected_error import tracebacks_to
from autoscout.weekly_workflow import deterministic_adapters
from tests.test_issue_946_weekly_pm_pass import _stage as _pm_stage
from tests.test_issue_956_weekly_pw_pass import BODY
from tests.test_issue_956_weekly_pw_pass import CREDENTIALS
from tests.test_issue_956_weekly_pw_pass import NOW
from tests.test_issue_956_weekly_pw_pass import Mailbox
from tests.test_issue_956_weekly_pw_pass import _weekly_config as _pw_weekly_config
from tests.test_kirkus_listing import _pass_config
from tests.test_pm_precollection import FakeMailbox

RUN_DATE = "2026-11-15"
BOOK_TITLE = "The Unmistakable Secret Book Title"
LEAKED_KEY = "sk-" + "a1b2c3d4e5f6g7h8i9j0k1l2m3"


class _Transport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def __call__(self, subject: str, body: str) -> None:
        self.sent.append((subject, body))


def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: BaseException, *, scheduled: bool = True):
    import autoscout.cli.weekly as cli

    monkeypatch.setattr(weekly_workflow, "weekly_adapters", lambda *args, **kwargs: types.SimpleNamespace())
    monkeypatch.setattr(kirkus_precollection, "last_attempt", lambda config_path: None)

    def fake_run(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(cli, "run_weekly_workflow", fake_run)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    transport = _Transport()
    code = cli._run_weekly_command(
        tmp_path / "config.json", output_dir, run_date=RUN_DATE, scheduled=scheduled, report_transport=transport
    )
    return code, output_dir, transport


def _logged(output_dir: Path) -> list[dict[str, Any]]:
    return json.loads((output_dir / "workflow-log.json").read_text(encoding="utf-8"))


def _doctor(tmp_path: Path, output_dir: Path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"run_date": RUN_DATE, "sources": []}), encoding="utf-8")
    seams = DoctorSeams(
        today=lambda: date.fromisoformat(RUN_DATE),
        repo_inspect=lambda _allowlist: {"status": "ok", "branch": "main"},
        guard_status=lambda: {"guard_exists": False},
        codex_probe=lambda: {"codex_available": True},
        codex_compat_probe=lambda: {"codex_compatible": True},
    )
    return diagnose(config, output_dir, seams=seams)


@pytest.mark.parametrize("scheduled", [True, False])
def test_an_unexpected_error_in_a_run_leaves_its_traceback_and_its_kind(monkeypatch, tmp_path, capsys, scheduled):
    error = RuntimeError(f"broke on {BOOK_TITLE} with {LEAKED_KEY}")
    code, output_dir, transport = _run(monkeypatch, tmp_path, error, scheduled=scheduled)
    captured = capsys.readouterr()

    assert code == 1
    # The operator reads the traceback in stderr (the journal and the per-run log), secrets redacted.
    assert "Traceback (most recent call last)" in captured.err and "RuntimeError" in captured.err
    assert "fake_run" in captured.err and BOOK_TITLE in captured.err
    assert LEAKED_KEY not in captured.err and "[REDACTED:openai_key]" in captured.err
    # The run log names the kind and the location, never the message or an absolute path.
    entry = _logged(output_dir)[-1]
    assert (entry["stage"], entry["status"]) == (UNEXPECTED_ERROR_STAGE, "failed")
    assert entry["detail"]["exception_type"] == "RuntimeError"
    assert entry["detail"]["exception_location"].startswith("tests/test_issue_1158_keep_traceback.py:")
    assert BOOK_TITLE not in json.dumps(entry) and LEAKED_KEY not in json.dumps(entry)
    assert str(tmp_path) not in json.dumps(entry)
    # The editor's text is what it was: the generic line, no traceback, no path.
    assert intervention_message("unknown") in captured.out.splitlines()
    assert "Traceback" not in captured.out and "RuntimeError" not in captured.out
    for _subject, body in transport.sent:
        assert "Traceback" not in body and "RuntimeError" not in body and str(tmp_path) not in body


def test_the_editor_text_of_a_scheduled_run_is_unchanged(monkeypatch, tmp_path, capsys):
    code, _output_dir, transport = _run(monkeypatch, tmp_path, RuntimeError("boom"))
    out = capsys.readouterr().out.splitlines()

    assert code == 1
    assert out[0] == intervention_message("unknown")
    assert out[1] == "Остановлено на этапе unknown, причина: unexpected_error."
    assert (
        len(transport.sent) == 1 and "Остановлено на этапе unknown, причина: unexpected_error." in transport.sent[0][1]
    )


def test_the_doctor_after_such_a_run_names_the_exception_type(monkeypatch, tmp_path):
    _code, output_dir, _transport = _run(monkeypatch, tmp_path, RuntimeError(f"broke on {BOOK_TITLE}"))

    report = _doctor(tmp_path, output_dir)

    assert report.facts["unexpected_error"]["exception_type"] == "RuntimeError"
    assert report.facts["unexpected_error"]["exception_location"].endswith(" in fake_run")
    text = render_russian(report)
    assert "Последняя остановка: непредвиденная ошибка RuntimeError." in text
    assert BOOK_TITLE not in text and "Traceback" not in text and str(tmp_path) not in text


def test_the_doctor_forgets_an_exception_the_run_went_on_after(monkeypatch, tmp_path):
    _code, output_dir, _transport = _run(monkeypatch, tmp_path, RuntimeError("boom"))
    log = _logged(output_dir)
    log.append({"stage": "capture", "status": "ok", "operation_id": log[-1]["operation_id"]})
    (output_dir / "workflow-log.json").write_text(json.dumps(log), encoding="utf-8")

    assert "unexpected_error" not in _doctor(tmp_path, output_dir).facts


def test_a_config_exit_keeps_what_the_workflow_printed_and_its_location(monkeypatch, tmp_path, capsys):
    import autoscout.cli.weekly as cli

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"sources": "not a list"}), encoding="utf-8")
    monkeypatch.setattr(weekly_workflow, "weekly_adapters", lambda *args, **kwargs: deterministic_adapters())
    output_dir = tmp_path / "run"
    transport = _Transport()

    code = cli._run_weekly_command(config, output_dir, run_date=RUN_DATE, scheduled=True, report_transport=transport)
    captured = capsys.readouterr()

    assert code == 1
    assert "config validation failed" in captured.err and "SystemExit" in captured.err
    entry = _logged(output_dir)[-1]
    assert entry["detail"]["exception_type"] == "SystemExit" and entry["detail"]["exit_code"] == 1
    assert entry["detail"]["exception_location"].startswith("autoscout/config.py:")
    assert "config validation failed" not in captured.out
    assert "Остановлено на этапе unknown, причина: config_invalid." in captured.out


def test_a_manual_config_exit_still_exits_and_keeps_what_the_workflow_printed(monkeypatch, tmp_path, capsys):
    import autoscout.cli.weekly as cli

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"sources": "not a list"}), encoding="utf-8")
    monkeypatch.setattr(weekly_workflow, "weekly_adapters", lambda *args, **kwargs: deterministic_adapters())

    with pytest.raises(SystemExit) as raised:
        cli._run_weekly_command(config, tmp_path / "run", run_date=RUN_DATE)

    assert raised.value.code == 1
    assert "config validation failed" in capsys.readouterr().err


def test_a_clean_run_leaves_no_failure_entry(monkeypatch, tmp_path, capsys):
    import autoscout.cli.weekly as cli

    monkeypatch.setattr(weekly_workflow, "weekly_adapters", lambda *args, **kwargs: types.SimpleNamespace())
    monkeypatch.setattr(
        cli,
        "run_weekly_workflow",
        lambda *a, **k: weekly_workflow.WeeklyWorkflowResult(
            outcome=weekly_workflow.WorkflowOutcome.COMPLETED_NO_ATTENTION
        ),
    )
    output_dir = tmp_path / "run"
    output_dir.mkdir()

    cli._run_weekly_command(tmp_path / "config.json", output_dir, run_date=RUN_DATE)

    assert not (output_dir / "workflow-log.json").exists()
    assert "Traceback" not in capsys.readouterr().err


def test_tracebacks_reach_the_original_stderr_while_stderr_is_silenced(capsys):
    import contextlib
    import io
    import sys

    from autoscout.unexpected_error import emit_traceback

    try:
        raise ValueError("kept")
    except ValueError as exc:
        with tracebacks_to(sys.stderr), contextlib.redirect_stderr(io.StringIO()):
            emit_traceback(exc, "a pass")

    err = capsys.readouterr().err
    assert "Unexpected error in a pass" in err and "ValueError: kept" in err


def test_the_location_names_a_file_of_the_project_without_an_absolute_path():
    try:
        raise ValueError
    except ValueError as exc:
        location = exception_location(exc)

    assert location.startswith("tests/test_issue_1158_keep_traceback.py:") or location.startswith("tests/")
    assert not location.startswith("/")


# ── The passes ──────────────────────────────────────────────────────────────


def test_a_kirkus_pass_that_dies_unexpectedly_keeps_its_traceback_and_location(tmp_path, capsys):
    def broken(url: str) -> Any:
        raise RuntimeError(f"socket closed {LEAKED_KEY}")

    report = kirkus_precollection.weekly_stage(
        _pass_config(tmp_path), tmp_path / "run", date(2026, 10, 14), fetch=broken, pause=lambda seconds: None
    )
    err = capsys.readouterr().err

    assert report["reason_code"] == "unexpected_error" and report["exception_type"] == "RuntimeError"
    assert report["exception_location"].endswith(" in broken")
    assert "Unexpected error in kirkus-precollection" in err and "socket closed" in err
    assert LEAKED_KEY not in err


def test_a_scheduled_kirkus_command_keeps_the_traceback_of_an_unexpected_error(tmp_path, monkeypatch, capsys):
    def crashes(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"crashed on {BOOK_TITLE}")

    monkeypatch.setattr(kirkus_precollection, "run_pass", crashes)

    code = kirkus_precollection.collect_kirkus_command(_pass_config(tmp_path), scheduled=True, today=date(2026, 10, 14))
    captured = capsys.readouterr()

    assert code == 1
    assert f"crashed on {BOOK_TITLE}" in captured.err and "Traceback" in captured.err
    assert BOOK_TITLE not in captured.out and "Traceback" not in captured.out


def test_a_pm_pass_that_dies_unexpectedly_keeps_its_traceback_and_location(tmp_path, capsys):
    class Broken(FakeMailbox):
        def headers_since(self, since: Any, senders: Any) -> Any:
            raise ZeroDivisionError

    report = _pm_stage(tmp_path, Broken())
    err = capsys.readouterr().err

    assert report["reason_code"] == "unexpected_error" and report["exception_type"] == "ZeroDivisionError"
    assert report["exception_location"].endswith(" in headers_since")
    assert "Unexpected error in pm-precollection" in err and "ZeroDivisionError" in err


def test_a_pw_pass_that_dies_unexpectedly_keeps_its_traceback_and_location(tmp_path, capsys):
    from autoscout.pw_precollection import weekly_stage

    class Broken(Mailbox):
        def headers_since(self, since: Any, senders: Any) -> Any:
            raise ZeroDivisionError

    output_dir = tmp_path / "run"
    output_dir.mkdir()
    report = weekly_stage(
        _pw_weekly_config(tmp_path),
        output_dir,
        opener=lambda *_args: Broken([], BODY),
        environ=CREDENTIALS,
        now=NOW,
    )
    err = capsys.readouterr().err

    assert report["reason_code"] == "unexpected_error" and report["exception_type"] == "ZeroDivisionError"
    assert report["exception_location"].endswith(" in headers_since")
    assert "Unexpected error in pw-precollection" in err and "ZeroDivisionError" in err
    attempt = json.loads((tmp_path / "data" / "pw" / "attempt.json").read_text(encoding="utf-8"))
    assert attempt["exception_type"] == "ZeroDivisionError"


# ── What a review of the first version found ────────────────────────────────


def test_a_config_whose_portfolio_is_missing_still_sends_the_morning_report(monkeypatch, tmp_path, capsys):
    import autoscout.cli.weekly as cli

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"weekly_portfolio": "missing.json"}), encoding="utf-8")
    monkeypatch.setattr(weekly_workflow, "weekly_adapters", lambda *args, **kwargs: deterministic_adapters())
    output_dir = tmp_path / "run"
    transport = _Transport()

    code = cli._run_weekly_command(config, output_dir, run_date=RUN_DATE, scheduled=True, report_transport=transport)

    assert code == 1
    assert len(transport.sent) == 1 and "config_invalid" in transport.sent[0][1]
    assert "weekly portfolio file not found" in capsys.readouterr().err


def test_a_run_records_what_escapes_it_once_while_it_holds_the_lock(monkeypatch, tmp_path):
    import autoscout.cli.weekly as cli

    class Boom(weekly_workflow.WeeklyWorkflow):
        def _run(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("inside the workflow")

    monkeypatch.setattr(weekly_workflow, "weekly_adapters", lambda *args, **kwargs: deterministic_adapters())
    monkeypatch.setattr(weekly_workflow, "WeeklyWorkflow", Boom)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"run_date": RUN_DATE, "sources": []}), encoding="utf-8")
    output_dir = tmp_path / "run"
    output_dir.mkdir()

    code = cli._run_weekly_command(config, output_dir, run_date=RUN_DATE, scheduled=True, report_transport=_Transport())

    assert code == 1
    entries = [e for e in _logged(output_dir) if e["stage"] == UNEXPECTED_ERROR_STAGE]
    assert len(entries) == 1 and entries[0]["detail"]["exception_type"] == "RuntimeError"


def test_the_doctor_calls_a_run_that_died_before_any_work_a_failure_not_a_healthy_start(monkeypatch, tmp_path):
    _code, output_dir, _transport = _run(monkeypatch, tmp_path, RuntimeError("boom"))

    report = _doctor(tmp_path, output_dir)

    assert report.diagnosis is DiagnosisClass.ARTIFACT_FAILURE and exit_code_for(report) != 0
    text = render_russian(report)
    assert "Последняя остановка: непредвиденная ошибка RuntimeError." in text
    assert "ещё не запускался" not in text


def test_a_pw_exception_that_echoes_a_subscriber_link_is_not_kept_in_the_journal(tmp_path, capsys):
    from autoscout.pw_precollection import weekly_stage

    link = "https://reader.publishersweekly.com/publishers_weekly/20261103/index.html?subscriber=abc123"

    class Echoing(Mailbox):
        def headers_since(self, since: Any, senders: Any) -> Any:
            raise RuntimeError(f"download failed for {link}")

    output_dir = tmp_path / "run"
    output_dir.mkdir()
    report = weekly_stage(
        _pw_weekly_config(tmp_path),
        output_dir,
        opener=lambda *_args: Echoing([], BODY),
        environ=CREDENTIALS,
        now=NOW,
    )
    err = capsys.readouterr().err

    assert report["exception_type"] == "RuntimeError"
    assert "abc123" not in err and "subscriber=" not in err
    assert "RuntimeError: <message omitted>" in err and "headers_since" in err


def test_an_exception_that_escapes_the_kirkus_worker_keeps_its_traceback(monkeypatch, tmp_path, capsys):
    from concurrent.futures import Future

    from autoscout.unexpected_error import keep_exception

    future: Future[Any] = Future()
    future.set_exception(RuntimeError("after submission"))
    try:
        future.result()
    except Exception as exc:
        facts = keep_exception(exc, "kirkus-precollection")

    assert facts["exception_type"] == "RuntimeError"
    assert "after submission" in capsys.readouterr().err


def test_a_failure_outside_the_workflow_does_not_write_the_log_of_a_run_that_holds_the_lock(monkeypatch, tmp_path):
    import autoscout.cli.weekly as cli

    monkeypatch.setattr(cli, "_another_run_holds_the_lock", lambda config_path: True)

    _code, output_dir, _transport = _run(monkeypatch, tmp_path, RuntimeError("boom"))

    assert not (output_dir / "workflow-log.json").exists()


def test_a_pw_programming_error_keeps_its_frames_and_kind_but_not_its_message(tmp_path, capsys):
    from autoscout.pw_precollection import weekly_stage

    secret = "subscriber=abc123"

    class Broken(Mailbox):
        def headers_since(self, since: Any, senders: Any) -> Any:
            raise KeyError(secret)

    output_dir = tmp_path / "run"
    output_dir.mkdir()
    report = weekly_stage(
        _pw_weekly_config(tmp_path),
        output_dir,
        opener=lambda *_args: Broken([], BODY),
        environ=CREDENTIALS,
        now=NOW,
    )
    err = capsys.readouterr().err

    assert report["reason_code"] == "pw_collection_failed" and report["exception_type"] == "KeyError"
    assert "headers_since" in err and "abc123" not in err


def test_the_doctor_keeps_the_review_availability_when_it_turns_healthy_into_a_failure(monkeypatch, tmp_path):
    from autoscout import doctor

    _code, output_dir, _transport = _run(monkeypatch, tmp_path, RuntimeError("boom"))
    healthy = doctor.DoctorReport(diagnosis=DiagnosisClass.HEALTHY, verdict="ok", review_available=True)
    monkeypatch.setattr(doctor, "_diagnose", lambda *args, **kwargs: healthy)

    report = _doctor(tmp_path, output_dir)

    assert report.diagnosis is DiagnosisClass.ARTIFACT_FAILURE
    assert report.review_available is True and "Обзор доступен." in render_russian(report)


def test_a_kirkus_request_thread_that_dies_keeps_its_traceback(monkeypatch, capsys):
    import http.client

    def bad_status(*_args: Any, **_kwargs: Any) -> Any:
        raise http.client.BadStatusLine("garbage")

    monkeypatch.setattr(kirkus_precollection.kirkus_listing, "open_without_redirect", bad_status)

    page = kirkus_precollection.fetch_page("https://www.kirkusreviews.com/x")

    assert page["status"] is None
    assert "BadStatusLine" in capsys.readouterr().err
