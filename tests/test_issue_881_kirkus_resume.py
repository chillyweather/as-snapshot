"""Recover each boundary of the deferred Kirkus batch without repeating main work."""

import hashlib
import json

import pytest

from autoscout import codex_workflow
from autoscout import source_collection
from autoscout.doctor import DiagnosisClass
from autoscout.doctor import DoctorSeams
from autoscout.doctor import diagnose
from autoscout.doctor_recovery import RecoverySeams
from autoscout.doctor_recovery import run_recovery
from autoscout.weekly_report import WeeklyReportStore
from autoscout.weekly_workflow import WeeklyWorkflow
from tests.test_issue_880_parallel_kirkus import _adapters
from tests.test_issue_880_parallel_kirkus import _config
from tests.test_issue_880_parallel_kirkus import _offline_other_capture  # noqa: F401
from tests.test_issue_880_parallel_kirkus import _Pass
from tests.test_kirkus_listing import REFERENCE
from tests.test_kirkus_listing import SOURCE


class Crash(BaseException):
    """A process interruption must bypass ordinary source-failure handling."""


CRASH_POINTS = [
    "background",
    "main",
    "after-main",
    "capture",
    "capture-file",
    "worklist",
    "source-runs",
    "late",
    "late-validated",
]


def _interrupted(tmp_path, monkeypatch, point, *, outcome="collected"):
    config = _config(tmp_path)
    output = tmp_path / "run"
    stage = _Pass("before", outcome=outcome)
    adapters = _adapters(stage)
    with monkeypatch.context() as patch:
        if point == "background":
            # Write the real attempt record, then interrupt the main thread
            # while the pass is still inside its first request.

            def collect(*args, **options):
                from autoscout.kirkus_precollection import weekly_stage

                stage.calls += 1
                stage.started.set()
                assert stage.capture_started.wait(5)

                def interrupted_fetch(url):
                    stage.main_finished.set()  # attempt has been durably recorded
                    assert stage.finished.wait(5)
                    raise Crash()

                try:
                    return weekly_stage(*args, fetch=interrupted_fetch, pause=lambda _: None, **options)
                finally:
                    stage.finished.set()

            patch.setattr(stage, "collect", collect)
            # An older usable result survives the interrupted pass.
            from datetime import date

            from autoscout.kirkus_precollection import weekly_stage
            from tests.test_kirkus_listing import site

            fetch, _, _ = site()
            weekly_stage(config, output, date(2026, 10, 6), fetch=fetch, pause=lambda _: None)

            def stop_main(*args, **kwargs):
                assert stage.main_finished.wait(5)
                stage.finished.set()
                raise Crash()

            patch.setattr(adapters.codex, "fulfill", stop_main)
        elif point == "main":

            def stop_main(output_dir, **kwargs):
                assert stage.finished.wait(5)
                item = json.loads((output / "codex-extraction-worklist.json").read_text())["work_items"][0]
                adapters.codex.agent.extract(
                    item, output / item["capture_file"], output / item["expected_extraction_file"]
                )
                raise Crash()

            patch.setattr(adapters.codex, "fulfill", stop_main)
        elif point == "after-main":
            original = adapters.codex.fulfill

            def stop_after_main(*args, **kwargs):
                original(*args, **kwargs)
                raise Crash()

            patch.setattr(adapters.codex, "fulfill", stop_after_main)
        elif point == "capture":
            original = adapters.capture.prepare

            def stop_capture(*args, **kwargs):
                if kwargs.get("kirkus_mode") == "only":
                    raise Crash()
                return original(*args, **kwargs)

            patch.setattr(adapters.capture, "prepare", stop_capture)
        elif point == "capture-file":
            original = source_collection.write_bounded_capture

            def stop_capture_file(payload, **options):
                result = original(payload, **options)
                if payload.get("source_name") == SOURCE["name"]:
                    raise Crash()
                return result

            patch.setattr(source_collection, "write_bounded_capture", stop_capture_file)
        elif point in {"worklist", "source-runs"}:
            original = codex_workflow.write_json

            def stop_write(path, payload):
                original(path, payload)
                if path.name == f"{'codex-extraction-worklist' if point == 'worklist' else 'source-runs'}.json":
                    worklist = json.loads((output / "codex-extraction-worklist.json").read_text())
                    if worklist.get("kirkus_pending") is False:
                        raise Crash()

            patch.setattr(codex_workflow, "write_json", stop_write)
        else:
            original = adapters.codex.fulfill_additional

            def stop_late(*args, **kwargs):
                if point == "late-validated":
                    original(*args, **kwargs)
                raise Crash()

            patch.setattr(adapters.codex, "fulfill_additional", stop_late)
        with pytest.raises(Crash):
            WeeklyWorkflow(adapters).run(config, output)
    assert stage.finished.wait(5)
    return config, output, stage, adapters


def _main_bytes(output):
    worklist = json.loads((output / "codex-extraction-worklist.json").read_text())
    item = next(item for item in worklist["work_items"] if item["source_name"] == "Test Source")
    return {
        key: (output / item[key]).read_bytes()
        for key in ("capture_file", "expected_extraction_file")
        if (output / item[key]).exists()
    }


@pytest.mark.parametrize("point", CRASH_POINTS)
def test_resume_completes_kirkus_once_and_preserves_main(tmp_path, monkeypatch, point):
    config, output, stage, adapters = _interrupted(tmp_path, monkeypatch, point)
    before = _main_bytes(output)
    capture_calls = list(adapters.capture.calls)
    main_calls = adapters.codex.agent.calls.count("Test Source")
    result = WeeklyWorkflow(adapters).run(config, output)
    assert result.outcome.value.startswith("completed/"), result.failed_stage
    assert stage.calls == 1
    assert adapters.capture.calls.count("defer") == 1
    assert adapters.capture.calls.count("all") == 0
    assert adapters.capture.calls.count("only") == (2 if point == "capture-file" else 1)
    assert adapters.capture.calls[: len(capture_calls)] == capture_calls
    assert adapters.codex.agent.calls.count("Test Source") == max(1, main_calls)
    assert adapters.codex.agent.calls.count(SOURCE["name"]) == 1
    assert before.items() <= _main_bytes(output).items()
    worklist = json.loads((output / "codex-extraction-worklist.json").read_text())
    assert worklist["kirkus_pending"] is False
    assert len(worklist["work_items"]) == 2
    candidates = WeeklyReportStore(output).read("scouting-candidates.json", [])
    assert any(row["title"] == "THE BULLWHIP'S ALGORITHM" for row in candidates)
    if point == "background":
        assert any(row["source_name"] == SOURCE["name"] for row in result.source_problems)


def _snapshot(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("point", CRASH_POINTS)
def test_doctor_readonly_preview_and_confirmed_recovery(tmp_path, monkeypatch, point):
    config, output, stage, adapters = _interrupted(tmp_path, monkeypatch, point)
    seams = DoctorSeams(
        today=lambda: REFERENCE,
        repo_inspect=lambda allowlist: {"status": "ok"},
        guard_status=lambda: {},
        codex_probe=lambda: {"codex_available": True},
        codex_compat_probe=lambda: {"codex_compatible": True},
    )
    before = _snapshot(tmp_path)
    report = diagnose(config, output, None, seams=seams)
    assert report.diagnosis == DiagnosisClass.RESUMABLE_OPERATION
    calls = []

    def resume(c, o, s):
        calls.append(True)
        return WeeklyWorkflow(adapters).run(c, o)

    recovery = RecoverySeams(doctor_seams=seams, workflow_runner=resume)
    preview, code = run_recovery(config, output, None, seams=recovery)
    assert code == 3 and preview.confirmation_required
    assert not calls and _snapshot(tmp_path) == before
    applied, code = run_recovery(config, output, None, confirmed_by_editor=True, seams=recovery)
    assert code in {0, 2}, applied.to_dict()
    assert applied.execution["outcome"].startswith("completed/")
    assert calls == [True] and stage.calls == 1


@pytest.mark.parametrize("outcome", ["partial", "raised", "exception"])
def test_resumed_pass_shortfalls_are_named_and_never_block(tmp_path, monkeypatch, outcome):
    config, output, stage, adapters = _interrupted(tmp_path, monkeypatch, "after-main", outcome=outcome)
    result = WeeklyWorkflow(adapters).run(config, output)
    assert result.outcome.value.startswith("completed/")
    assert any(problem["source_name"] == SOURCE["name"] for problem in result.source_problems)
    assert stage.calls == 1 and adapters.capture.calls == ["defer", "only"]
    assert adapters.codex.agent.calls.count("Test Source") == 1
    assert not json.loads((output / "codex-extraction-worklist.json").read_text())["kirkus_pending"]


def test_kirkus_failure_during_resumed_extraction_does_not_stop_the_run(tmp_path, monkeypatch):
    config, output, stage, adapters = _interrupted(tmp_path, monkeypatch, "late")
    original = adapters.codex.agent.extract

    def fail_kirkus(item, *args):
        if item["source_name"] == SOURCE["name"]:
            raise RuntimeError("Kirkus extraction failed")
        return original(item, *args)

    monkeypatch.setattr(adapters.codex.agent, "extract", fail_kirkus)
    result = WeeklyWorkflow(adapters).run(config, output)
    assert result.outcome.value.startswith("completed/")
    assert any(problem["source_name"] == SOURCE["name"] for problem in result.source_problems)
    assert stage.calls == 1 and adapters.codex.agent.calls.count("Test Source") == 1


@pytest.mark.parametrize("corruption", ["pending", "missing-pending", "main-runs", "capture-hash", "late-runs", "date"])
def test_inconsistent_late_capture_stays_fail_closed(tmp_path, monkeypatch, corruption):
    from autoscout.mvp_run_evidence import inspect

    config, output, stage, adapters = _interrupted(tmp_path, monkeypatch, "worklist")
    path = output / "codex-extraction-worklist.json"
    worklist = json.loads(path.read_text())
    if corruption == "pending":
        worklist["kirkus_pending"] = True
    elif corruption == "missing-pending":
        del worklist["kirkus_pending"]
    elif corruption == "main-runs":
        (output / "source-runs.json").write_text("[]")
    elif corruption == "capture-hash":
        worklist["work_items"][-1]["capture_sha256"] = "bad"
    elif corruption == "late-runs":
        worklist["kirkus_source_runs"][0]["source_name"] = "Test Source"
    elif corruption == "date":
        worklist["kirkus_source_runs"][0]["source_check_date"] = "2026-01-01"
    path.write_text(json.dumps(worklist))
    evidence = inspect(output, config, REFERENCE)
    assert not evidence.capture_current and not evidence.resumable and not evidence.allowlist
    before = _main_bytes(output)
    calls = list(adapters.capture.calls)
    result = WeeklyWorkflow(adapters).run(config, output)
    assert result.outcome.value == "intervention-required"
    assert adapters.capture.calls == calls and stage.calls == 1
    assert _main_bytes(output) == before


@pytest.mark.parametrize("point", ["main", "capture-file", "worklist", "late"])
def test_real_repository_preflight_accepts_only_owned_recovery_files(tmp_path, monkeypatch, point):
    import subprocess

    from autoscout.git_envelope import GitRepositoryAdapter
    from autoscout.mvp_run_evidence import inspect
    from tests.test_issue_315_retry_blocked import _add_local_remote
    from tests.test_issue_315_retry_blocked import _init_git_repo

    config, output, _, adapters = _interrupted(tmp_path, monkeypatch, point)
    _init_git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("/state/\nprogress-snapshot.json\nkirkus-progress-snapshot.json\n")
    for path in tmp_path.iterdir():
        if path.name not in {"run", "state", ".git"}:
            subprocess.run(["git", "add", "--", str(path)], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "test config"], cwd=tmp_path, check=True, capture_output=True)
    _add_local_remote(tmp_path, tmp_path.parent / f"remote-{point}.git")
    repository = GitRepositoryAdapter(tmp_path)
    evidence = inspect(output, config, REFERENCE)
    assert evidence.capture_current
    assert repository.inspect_local(set(evidence.allowlist))["status"] == "ok"
    notes = output / "editor-notes.txt"
    notes.write_text("Unrelated edits must still block recovery")
    assert repository.inspect_local(set(evidence.allowlist))["status"] != "ok"
    notes.unlink()
    adapters.repository = repository
    result = WeeklyWorkflow(adapters).run(config, output)
    assert result.outcome.value.startswith("completed/"), result.failed_stage
    assert adapters.codex.agent.calls.count("Test Source") == 1
