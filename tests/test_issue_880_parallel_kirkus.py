"""The daily pass overlaps capture/extraction and rejoins the same operation (#880)."""

import json
import threading
from datetime import date

import pytest

from autoscout import source_collection
from autoscout.codex_recovery import CodexResumeAdapter
from autoscout.codex_recovery import ExtractionAttempt
from autoscout.codex_recovery import ExtractionStatus
from autoscout.codex_recovery import ProductionCodexAdapter
from autoscout.codex_recovery import ProductionCodexCaptureAdapter
from autoscout.codex_workflow import prepare_codex_extraction
from autoscout.kirkus_precollection import weekly_stage
from autoscout.mvp_run_evidence import bind_worklist_to_operation
from autoscout.mvp_run_evidence import inspect as inspect_evidence
from autoscout.mvp_run_evidence import operation_id
from autoscout.runtime_budget import FINALIZATION_RESERVE_SECONDS
from autoscout.runtime_budget import WEEKLY_OPERATION_CEILING_SECONDS
from autoscout.weekly_report import WeeklyReportStore
from autoscout.weekly_workflow import WeeklyWorkflow
from autoscout.weekly_workflow import deterministic_adapters
from tests.test_kirkus_listing import REFERENCE
from tests.test_kirkus_listing import SOURCE
from tests.test_kirkus_listing import _clock
from tests.test_kirkus_listing import _pass_config
from tests.test_kirkus_listing import site
from tests.test_weekly_workflow import _write_capture_fixture
from tests.test_weekly_workflow import _write_codex_fixture_config


@pytest.fixture(autouse=True)
def _offline_other_capture(monkeypatch):
    original = source_collection.codex_source_page

    def capture(config, output_dir, source, *, live=False):
        return original(config, output_dir, source, live=False if source["name"] == "Test Source" else live)

    monkeypatch.setattr(source_collection, "codex_source_page", capture)


def _config(tmp_path):
    other = json.loads(_write_codex_fixture_config(tmp_path).read_text())
    other["sources"][0]["source_page_fixture_file"] = "../test-capture.json"
    _write_capture_fixture(tmp_path, tmp_path / "run")
    config = _pass_config(tmp_path)
    kirkus = json.loads(config.read_text())["sources"][0]
    other["sources"].append(kirkus)
    config.write_text(json.dumps(other))
    return config


class _Pass:
    def __init__(self, order, *, outcome="collected"):
        self.order = order
        self.outcome = outcome
        self.started = threading.Event()
        self.capture_started = threading.Event()
        self.main_finished = threading.Event()
        self.finished = threading.Event()
        self.calls = 0
        self.report = None

    def collect(self, config, output_dir, reference, **options):
        self.calls += 1
        self.started.set()
        assert options["write_snapshot"] is False
        assert self.capture_started.wait(5), "capture waited for Kirkus"
        if self.order == "after":
            assert self.main_finished.wait(5), "main extraction waited for Kirkus"
        fetch, _, _ = site()

        def read(url):
            if self.outcome == "raised":
                raise RuntimeError("test pass failed")
            return fetch(url)

        # A fake clock consumes twenty seconds per pause, without real sleeps.
        elapsed = [0.0]
        options["budget_seconds"] = min(options["budget_seconds"], 245 if self.outcome == "partial" else 2400)
        try:
            if self.outcome == "exception":
                raise RuntimeError("test adapter raised")
            self.report = weekly_stage(
                config,
                output_dir,
                reference,
                fetch=read,
                pause=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
                monotonic=lambda: elapsed[0],
                **options,
            )
            return self.report
        finally:
            self.finished.set()


class _Capture(ProductionCodexCaptureAdapter):
    def __init__(self, stage):
        super().__init__()
        self.stage = stage
        self.calls = []

    def prepare(self, config, output_dir, **options):
        mode = options.get("kirkus_mode", "all")
        self.calls.append(mode)
        if mode == "defer":
            assert self.stage.started.wait(5)
            assert not self.stage.finished.is_set()
            self.stage.capture_started.set()
        elif mode == "only":
            assert self.stage.finished.is_set()
        return super().prepare(config, output_dir, **options)


class _Agent:
    def __init__(self, stage):
        self.stage = stage
        self.calls = []

    def extract(self, item, capture_path, artifact_path):
        self.calls.append(item["source_name"])
        if item["source_name"] == SOURCE["name"]:
            assert self.stage.main_finished.is_set()
            rows = json.loads(json.loads(capture_path.read_text())["text"])
            entries = [
                {
                    **row,
                    "source_url": SOURCE["url"],
                    "signal_type": "media/curated list",
                    "signal_strength": "minor mention",
                    "candidate_category": "adult nonfiction",
                    "evidence_note": "Reviewed book row on the captured coming-soon listing.",
                }
                for row in rows
            ]
        else:
            if self.stage.order == "before":
                assert self.stage.finished.wait(5)
            else:
                assert not self.stage.finished.is_set()
            entries = [
                {
                    "title": "Test Book",
                    "primary_authors": ["Test Author"],
                    "source_url": "https://example.test/book",
                    "evidence_url": "https://example.test/book",
                    "evidence_note": "Test note.",
                    "candidate_category": "popular science",
                    "signal_type": "award shortlist",
                    "signal_strength": "shortlist",
                }
            ]
        artifact_path.write_text(
            json.dumps(
                {
                    **{key: item[key] for key in ("source_name", "source_url", "operation_id", "capture_sha256")},
                    "entries": entries,
                }
            )
        )
        return ExtractionAttempt(ExtractionStatus.PRODUCED, False, 0)


class _Codex(ProductionCodexAdapter):
    def __init__(self, stage):
        self.stage = stage
        self.agent = _Agent(stage)
        super().__init__(agent=self.agent, max_attempts=1)

    def fulfill(self, output_dir, **options):
        result = super().fulfill(output_dir, **options)
        if "source_urls" not in options:
            self.stage.main_finished.set()
        return result


def _adapters(stage):
    adapters = deterministic_adapters()
    adapters.clock = _clock(REFERENCE)
    adapters.kirkus_precollection = stage
    adapters.capture = _Capture(stage)
    adapters.codex = _Codex(stage)
    return adapters


@pytest.mark.parametrize("order", ["before", "after"])
@pytest.mark.parametrize("outcome", ["collected", "partial", "raised", "exception"])
def test_pass_overlaps_main_batch_and_its_rows_reach_candidates(tmp_path, order, outcome):
    config = _config(tmp_path)
    output_dir = tmp_path / "run"
    if outcome in {"raised", "exception"}:
        fetch, _, _ = site()
        weekly_stage(config, output_dir, date(2026, 10, 6), fetch=fetch, pause=lambda seconds: None)
    stage = _Pass(order, outcome=outcome)
    adapters = _adapters(stage)
    result = WeeklyWorkflow(adapters).run(config, output_dir)
    assert result.outcome.value.startswith("completed/"), result.failed_stage
    assert adapters.capture.calls == ["defer", "only"]
    assert adapters.codex.agent.calls == ["Test Source", SOURCE["name"]]
    candidates = WeeklyReportStore(output_dir).read("scouting-candidates.json", default=[])
    assert any(candidate["title"] == "THE BULLWHIP'S ALGORITHM" for candidate in candidates)
    worklist = json.loads((output_dir / "codex-extraction-worklist.json").read_text())
    assert len(worklist["work_items"]) == 2 and not worklist["kirkus_pending"]
    assert {item["operation_id"] for item in worklist["work_items"]} == {operation_id(config, REFERENCE)}
    outcomes = json.loads((output_dir / "codex-extraction-outcomes.json").read_text())["outcomes"]
    assert {entry["source_name"] for entry in outcomes} == {"Test Source", SOURCE["name"]}
    if outcome != "exception":
        assert stage.report["status"] == ("not_collected" if outcome == "raised" else outcome)
    if outcome == "partial":
        assert stage.report["stop_reason"] == "kirkus_time_budget"
    if outcome != "collected":
        assert any(problem["source_name"] == SOURCE["name"] for problem in result.source_problems)
    log = json.loads((output_dir / "workflow-log.json").read_text())
    extraction = next(entry["detail"] for entry in log if entry["stage"] == "codex")
    assert extraction["ready_items"] == 2 and extraction["total_items"] == 2
    start = next(i for i, entry in enumerate(log) if entry["stage"] == "kirkus-precollection")
    main = next(i for i, entry in enumerate(log) if entry["stage"] == "codex-main")
    finish = next(
        i for i, entry in enumerate(log) if entry["stage"] == "kirkus-precollection" and entry["status"] == "ok"
    )
    assert start < main < finish
    if outcome == "exception":
        detail = log[finish]["detail"]
        assert (detail["status"], detail["exception_type"]) == ("not_collected", "RuntimeError")
        assert detail["exception_location"].endswith(" in collect")  # the traceback is in stderr (#1158)


def test_background_heartbeat_does_not_replace_the_main_snapshot(tmp_path):
    config = _pass_config(tmp_path)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    path = output_dir / "progress-snapshot.json"
    path.write_text('{"stage":"extraction","status":"running"}')
    before = path.read_bytes()
    lines = []
    fetch, _, _ = site()
    weekly_stage(
        config,
        output_dir,
        REFERENCE,
        fetch=fetch,
        pause=lambda seconds: None,
        write_snapshot=False,
        presentation=lines.append,
    )
    assert path.read_bytes() == before
    assert lines and lines[0]["stage"] == "kirkus-precollection"


@pytest.mark.parametrize("status", ["collected", "partial", "not_collected"])
def test_no_background_pass_after_an_attempt_today(tmp_path, status):
    config = _config(tmp_path)
    path = tmp_path / "state" / "kirkus-collection-attempt.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"attempted_date": REFERENCE.isoformat(), "status": status}))
    stage = _Pass("before")
    adapters = _adapters(stage)
    # No pass is due, so the regular capture path remains unchanged.
    adapters.codex = deterministic_adapters().codex
    result = WeeklyWorkflow(adapters).run(config, tmp_path / "run")
    assert stage.calls == 0
    assert adapters.capture.calls == ["all"]
    assert result.outcome.value.startswith("completed/")


@pytest.mark.parametrize("append_interrupted", [False, True])
def test_resume_never_restarts_the_pass_or_recaptures_others(tmp_path, append_interrupted):
    config = _config(tmp_path)
    output_dir = tmp_path / "run"
    prepare_codex_extraction(config, output_dir, live=True, effective_date=REFERENCE.isoformat(), kirkus_mode="defer")
    run_id = operation_id(config, REFERENCE)
    bind_worklist_to_operation(output_dir, run_id)
    CodexResumeAdapter(output_dir).mark_stage_complete(output_dir, "capture", run_id)
    assert inspect_evidence(output_dir, config, REFERENCE).capture_current
    (output_dir / "workflow-log.json").write_text(
        json.dumps(
            [
                {"stage": "kirkus-precollection", "status": "started", "operation_id": run_id},
                {"stage": "codex-main", "status": "ok", "operation_id": run_id},
            ]
        )
    )
    if append_interrupted:
        # The worklist was appended but the source-run write did not finish.
        (output_dir / "source-runs.json").write_text("[]")
    stage = _Pass("before")
    stage.finished.set()  # The previous process no longer owns a live pass.
    adapters = _adapters(stage)
    adapters.codex = deterministic_adapters().codex
    WeeklyWorkflow(adapters).run(config, output_dir)
    assert stage.calls == 0
    # #881 completes a consistent pending batch; missing main evidence still
    # fails closed instead of replacing the other sources' capture.
    assert adapters.capture.calls == ([] if append_interrupted else ["only"])


def test_late_capture_uses_only_remaining_source_budget(tmp_path, monkeypatch):
    config = _config(tmp_path)
    stage = _Pass("before")
    adapters = _adapters(stage)
    elapsed = [0.0]
    original = adapters.codex.fulfill

    def main_consumes_budget(output_dir, **options):
        result = original(output_dir, **options)
        if "source_urls" not in options:
            elapsed[0] = WEEKLY_OPERATION_CEILING_SECONDS - FINALIZATION_RESERVE_SECONDS
        return result

    monkeypatch.setattr(adapters.codex, "fulfill", main_consumes_budget)
    result = WeeklyWorkflow(adapters, monotonic=lambda: elapsed[0]).run(config, tmp_path / "run")
    assert result.outcome.value.startswith("completed/")
    assert adapters.codex.agent.calls == ["Test Source"]
    assert any(
        problem["source_name"] == SOURCE["name"] and "budget" in problem["reason"] for problem in result.source_problems
    )


def test_late_extraction_capacity_stop_names_kirkus_and_does_not_block_finalize(tmp_path, monkeypatch):
    config = _config(tmp_path)
    stage = _Pass("before")
    adapters = _adapters(stage)
    elapsed = [0.0]
    original = adapters.capture.prepare

    def capture_consumes_budget(config, output_dir, **options):
        result = original(config, output_dir, **options)
        if options.get("kirkus_mode") == "only":
            elapsed[0] = WEEKLY_OPERATION_CEILING_SECONDS - FINALIZATION_RESERVE_SECONDS
        return result

    monkeypatch.setattr(adapters.capture, "prepare", capture_consumes_budget)
    output_dir = tmp_path / "run"
    result = WeeklyWorkflow(adapters, monotonic=lambda: elapsed[0]).run(config, output_dir)
    assert result.outcome.value.startswith("completed/")
    assert adapters.codex.agent.calls == ["Test Source"]
    assert any(problem["source_name"] == SOURCE["name"] for problem in result.source_problems)
    outcomes = json.loads((output_dir / "codex-extraction-outcomes.json").read_text())["outcomes"]
    assert {entry["source_name"]: entry["terminal_class"] for entry in outcomes} == {
        "Test Source": "validated",
        SOURCE["name"]: "budget_exhausted",
    }
    replay = json.loads((output_dir / "codex-replay-config.json").read_text())
    assert any(source["name"] == "Test Source" and source["active"] for source in replay["sources"])


def test_interrupted_late_extraction_keeps_the_bound_worklist_on_resume(tmp_path, monkeypatch):
    config = _config(tmp_path)
    stage = _Pass("before")
    adapters = _adapters(stage)
    output_dir = tmp_path / "run"

    class Crash(BaseException):
        pass

    def crash(*args, **kwargs):
        raise Crash()

    monkeypatch.setattr(adapters.codex, "fulfill_additional", crash)
    with pytest.raises(Crash):
        WeeklyWorkflow(adapters).run(config, output_dir)
    assert inspect_evidence(output_dir, config, REFERENCE).capture_current
    worklist = (output_dir / "codex-extraction-worklist.json").read_bytes()
    calls = list(adapters.capture.calls)
    adapters.codex = ProductionCodexAdapter(agent=adapters.codex.agent, max_attempts=1)
    result = WeeklyWorkflow(adapters).run(config, output_dir)
    assert result.outcome.value.startswith("completed/")
    assert stage.calls == 1 and adapters.capture.calls == calls
    assert (output_dir / "codex-extraction-worklist.json").read_bytes() == worklist
    assert adapters.codex._agent.calls == ["Test Source", SOURCE["name"]]


def test_late_segment_stop_keeps_main_outcomes_and_allows_finalization(tmp_path, monkeypatch):
    config = _config(tmp_path)
    stage = _Pass("before")
    adapters = _adapters(stage)
    original = adapters.codex.fulfill

    def capacity_stop(output_dir, **options):
        if "source_urls" not in options:
            return original(output_dir, **options)
        # Segmented extraction writes a portfolio stop at a scheduling
        # boundary. It applies only to the late source in this call.
        (output_dir / "codex-extraction-outcomes.json").write_text(
            json.dumps(
                {
                    "outcomes": [
                        {
                            "source_name": SOURCE["name"],
                            "source_url": SOURCE["url"],
                            "terminal_class": "portfolio_capacity_stop",
                            "outcome": "absent",
                        }
                    ]
                }
            )
        )
        return {
            "status": "unfinished",
            "unfinished_items": 1,
            "source_timings": [
                {"source_name": SOURCE["name"], "status": "not_ready", "terminal": "portfolio_capacity_stop"}
            ],
        }

    monkeypatch.setattr(adapters.codex, "fulfill", capacity_stop)
    output_dir = tmp_path / "run"
    result = WeeklyWorkflow(adapters).run(config, output_dir)
    assert result.outcome.value.startswith("completed/")
    assert any(problem["source_name"] == SOURCE["name"] for problem in result.source_problems)
    outcomes = json.loads((output_dir / "codex-extraction-outcomes.json").read_text())["outcomes"]
    assert {entry["source_name"]: entry["terminal_class"] for entry in outcomes} == {
        "Test Source": "validated",
        SOURCE["name"]: "unavailable",
    }


def test_rerun_after_a_failed_main_capture_is_not_blocked(tmp_path):
    # A capture that failed (or a run killed during it) left only the pass's
    # "started" entry; recapturing discards nothing, so the rerun proceeds.
    config = _config(tmp_path)
    output_dir = tmp_path / "run"
    output_dir.mkdir(exist_ok=True)
    run_id = operation_id(config, REFERENCE)
    (output_dir / "workflow-log.json").write_text(
        json.dumps([{"stage": "kirkus-precollection", "status": "started", "operation_id": run_id}])
    )
    stage = _Pass("before")
    adapters = _adapters(stage)
    result = WeeklyWorkflow(adapters).run(config, output_dir)
    assert result.outcome.value.startswith("completed/"), result
