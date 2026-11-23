"""The Russian-edition pass inside the weekly run (#1181, PR 3 of 3).

Every test is offline: the sources are a fake fetcher answering recorded or
built responses, the model a fake matcher, and the State Store is seeded.
"""

from __future__ import annotations

import contextlib
import json
import time
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from russian_edition_support import BLOCKED
from russian_edition_support import TODAY
from russian_edition_support import FakeClock
from russian_edition_support import FakeFetcher
from russian_edition_support import FakeMatcher
from russian_edition_support import book
from russian_edition_support import by_id
from russian_edition_support import recorded
from russian_edition_support import routed
from russian_edition_support import workspace
from state_store_support import read_run_output
from test_issue_640_description_adjudication_stage import _write_codex_fixture_config

from autoscout import editorial_fit_shadow
from autoscout import run_time_limit
from autoscout import russian_edition_completion
from autoscout.config import validate_config
from autoscout.doctor import DoctorSeams
from autoscout.doctor import diagnose
from autoscout.doctor import render_russian
from autoscout.progress_snapshot import snapshot_stale_after_seconds
from autoscout.run_health import run_health
from autoscout.russian_edition_completion import SOURCE_FAILURE_LIMIT
from autoscout.russian_edition_completion import STAGE_NAME
from autoscout.russian_edition_completion import ProductionRussianEditionCompletionStage
from autoscout.russian_edition_health import SOURCE_FAILURE_LIMIT as HEALTH_FAILURE_LIMIT
from autoscout.russian_edition_health import STAGE_NAME as HEALTH_STAGE_NAME
from autoscout.russian_edition_settings import SWITCH
from autoscout.russian_edition_sources import SOURCE_LITRES
from autoscout.russian_edition_sources import SOURCE_LIVELIB
from autoscout.russian_edition_sources import SOURCE_RSL
from autoscout.russian_edition_sources import SOURCE_WIKIDATA
from autoscout.russian_workflow import render_workflow_result
from autoscout.russian_workflow import snapshot_progress_line
from autoscout.weekly_report import TRANSACTION_MARKER
from autoscout.weekly_report import WeeklyReportStore
from autoscout.weekly_workflow import STAGE_ORDER
from autoscout.weekly_workflow import bind_russian_edition_completion
from autoscout.weekly_workflow import deterministic_adapters
from autoscout.weekly_workflow import run_weekly_workflow

WALKER_TITLE = "Why We Sleep"
WALKER = "Matthew Walker"
RUSSIAN_TITLE = "Зачем мы спим"
RUN_DATE = "2026-07-13"


class TitleMatcher(FakeMatcher):
    """Names, for every book, the first numbered title starting with ``wanted``."""

    def __init__(self, wanted: str = RUSSIAN_TITLE, *, clock: FakeClock | None = None) -> None:
        super().__init__()
        self.wanted = wanted
        self.clock = clock

    def match(self, items: Any, *, timeout: float) -> Any:
        if self.clock is not None:
            # A model call takes a while.
            self.clock.now += 60.0
        self.picks = {item["candidate_id"]: self.wanted for item in items}
        return super().match(items, timeout=timeout)


class _RaisingStage:
    def complete(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("catalogue down")


# ── A weekly run of one book: "Why We Sleep" by Matthew Walker ───────────


def _write_capture(tmp_path: Path) -> None:
    (tmp_path / "test-capture.json").write_text(
        json.dumps(
            {
                "source_url": "https://example.test/source",
                "html": f'<html><body><a href="https://example.test/book">{WALKER_TITLE}</a> by {WALKER}</body></html>',
            }
        ),
        encoding="utf-8",
    )


def _write_extraction(output_dir: Path) -> None:
    worklist = json.loads((output_dir / "codex-extraction-worklist.json").read_text(encoding="utf-8"))
    item = worklist["work_items"][0]
    (output_dir / str(item["expected_extraction_file"])).write_text(
        json.dumps(
            {
                "source_name": "Test Source",
                "source_url": "https://example.test/source",
                "operation_id": item["operation_id"],
                "capture_sha256": item["capture_sha256"],
                "entries": [
                    {
                        "title": WALKER_TITLE,
                        "primary_authors": [WALKER],
                        "source_url": "https://example.test/book",
                        "evidence_url": "https://example.test/book",
                        "evidence_note": "Test note.",
                        "candidate_category": "popular science",
                        "signal_type": "award shortlist",
                        "signal_strength": "shortlist",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _config(tmp_path: Path, **keys: Any) -> Path:
    config_path = _write_codex_fixture_config(tmp_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.update(keys)
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


def _run_weekly(tmp_path: Path, adapters: Any, **keys: Any) -> tuple[Path, Any]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = _config(tmp_path, **keys)
    output_dir = tmp_path / "run"
    output_dir.mkdir(exist_ok=True)
    _write_capture(tmp_path)
    adapters.capture.prepare(config_path, output_dir, live=False)
    _write_extraction(output_dir)
    return output_dir, run_weekly_workflow(config_path, output_dir, adapters=adapters)


def _stage(fetcher: FakeFetcher, matcher: Any = None, clock: FakeClock | None = None) -> Any:
    clock = clock or FakeClock()
    return ProductionRussianEditionCompletionStage(
        fetcher_factory=lambda: fetcher,
        matcher_factory=lambda: matcher if matcher is not None else TitleMatcher(),
        monotonic=clock,
        sleeper=clock.sleep,
    )


def _entries(result: Any) -> list[dict[str, Any]]:
    return [entry for entry in result.log if entry.get("stage") == STAGE_NAME]


def _doctor(config_path: Path, output_dir: Path) -> Any:
    seams = DoctorSeams(
        today=lambda: date.fromisoformat(RUN_DATE),
        repo_inspect=lambda _allowlist: {"status": "ok", "branch": "main"},
        guard_status=lambda: {"guard_exists": False},
        codex_probe=lambda: {"codex_available": True},
        codex_compat_probe=lambda: {"codex_compatible": True},
    )
    return diagnose(config_path, output_dir, seams=seams)


# ── The switch ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("keys", [{}, {SWITCH: False}])
def test_without_the_switch_the_stage_never_runs_and_asks_nothing(tmp_path, keys):
    fetcher = FakeFetcher(recorded("why-we-sleep"))
    adapters = deterministic_adapters()
    adapters.russian_edition_completion = _stage(fetcher)

    output_dir, result = _run_weekly(tmp_path, adapters, **keys)

    assert fetcher.requests == []
    assert _entries(result) == []
    assert result.outcome.value.startswith("completed")
    assert "russian_edition" not in json.loads((output_dir / "scouting-candidates.json").read_text("utf-8"))[0]


def test_with_the_switch_on_the_weekly_run_marks_a_confirmed_edition_in_its_own_review(tmp_path):
    fetcher = FakeFetcher(recorded("why-we-sleep"))
    adapters = deterministic_adapters()
    adapters.russian_edition_completion = _stage(fetcher)

    output_dir, result = _run_weekly(tmp_path, adapters, **{SWITCH: True})

    (entry,) = _entries(result)
    assert entry["status"] == "ok"
    assert entry["detail"]["confirmed"] == 1
    assert set(entry["detail"]["sources"][SOURCE_RSL]) == {"answered", "blocked", "failed", "not_asked"}
    assert STAGE_ORDER.index("work-age-completion") < STAGE_ORDER.index(STAGE_NAME) < STAGE_ORDER.index("review")
    stages = [str(logged.get("stage")) for logged in result.log]
    assert stages.index(STAGE_NAME) < stages.index("review")
    (recorded_book,) = [
        record for record in read_run_output(output_dir, "candidate_history") if record.get("title") == WALKER_TITLE
    ]
    assert recorded_book["russian_edition"]["status"] == "confirmed"
    (current,) = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
    assert current["russian_edition"]["status"] == "confirmed"
    assert current["russian_edition"]["russian_title"].startswith(RUSSIAN_TITLE)
    html = (output_dir / "weekly-review.html").read_text(encoding="utf-8")
    assert "Есть русское издание" in html and RUSSIAN_TITLE in html
    assert result.outcome.value.startswith("completed")


def test_the_editor_facing_completion_output_is_the_same_with_the_stage_on_and_off(tmp_path):
    off_adapters = deterministic_adapters()
    off_adapters.russian_edition_completion = _stage(FakeFetcher(recorded("why-we-sleep")))
    on_adapters = deterministic_adapters()
    on_adapters.russian_edition_completion = _stage(FakeFetcher(recorded("why-we-sleep")))

    off_dir, off = _run_weekly(tmp_path / "off", off_adapters)
    on_dir, on = _run_weekly(tmp_path / "on", on_adapters, **{SWITCH: True})

    assert _entries(on) and not _entries(off)
    assert render_workflow_result(on, include_progress=False) == render_workflow_result(off, include_progress=False)
    assert (on.outcome, on.source_problems, on.delta_counts) == (off.outcome, off.source_problems, off.delta_counts)
    # The review differs only by the mark the PRD intends.
    off_review = (off_dir / "weekly-review.html").read_text(encoding="utf-8")
    on_review = (on_dir / "weekly-review.html").read_text(encoding="utf-8")
    assert "Есть русское издание" in on_review and "Есть русское издание" not in off_review


# ── No outcome stops the run ─────────────────────────────────────────────


def test_a_stage_that_raises_leaves_the_run_completed_and_is_named_for_the_operator(tmp_path, capsys):
    adapters = deterministic_adapters()
    adapters.russian_edition_completion = _RaisingStage()

    output_dir, result = _run_weekly(tmp_path, adapters, **{SWITCH: True})

    (entry,) = _entries(result)
    assert entry["status"] == "failed"
    assert entry["detail"] == {"status": "failed", "exception_type": "RuntimeError"}
    assert result.outcome.value.startswith("completed")
    assert (output_dir / "weekly-review.html").exists()

    run_health(output_dir)
    lines = capsys.readouterr().out.splitlines()
    assert "russian_edition_completion=failed" in lines
    assert "russian_edition_completion_problem=RuntimeError" in lines

    report = _doctor(tmp_path / "config.json", output_dir)
    assert report.facts["russian_edition"]["status"] == "failed"
    assert "Поиск русских изданий в последнем поиске не удался (RuntimeError)" in render_russian(report)


def test_every_source_blocked_leaves_the_run_completed_and_names_the_blocked_sources(tmp_path, capsys):
    fetcher = FakeFetcher(dict.fromkeys((SOURCE_LIVELIB, SOURCE_WIKIDATA, SOURCE_RSL, SOURCE_LITRES), BLOCKED))
    adapters = deterministic_adapters()
    adapters.russian_edition_completion = _stage(fetcher)

    output_dir, result = _run_weekly(tmp_path, adapters, **{SWITCH: True})

    (entry,) = _entries(result)
    assert entry["status"] == "ok"
    assert entry["detail"]["confirmed"] == 0
    assert entry["detail"]["sources"][SOURCE_LIVELIB]["blocked"] == 1
    assert result.outcome.value.startswith("completed")
    assert (output_dir / "weekly-review.html").exists()

    run_health(output_dir)
    lines = capsys.readouterr().out.splitlines()
    assert "russian_edition_completion=ok" in lines
    assert "russian_edition_source_problem=livelib|blocked=1|failed=0|not_asked=0" in lines

    text = render_russian(_doctor(tmp_path / "config.json", output_dir))
    assert "Поиск русских изданий: источники отвечали с проблемами: LiveLib — отказов 1" in text
    # Operator diagnostics only: the review carries no mark and no word about the block.
    assert "Есть русское издание" not in (output_dir / "weekly-review.html").read_text(encoding="utf-8")


def test_a_stage_that_ran_cleanly_adds_nothing_to_the_doctor(tmp_path, capsys):
    adapters = deterministic_adapters()
    adapters.russian_edition_completion = _stage(FakeFetcher(recorded("why-we-sleep")))

    output_dir, _result = _run_weekly(tmp_path, adapters, **{SWITCH: True})

    run_health(output_dir)
    lines = capsys.readouterr().out.splitlines()
    assert "russian_edition_completion=ok" in lines
    assert not any(line.startswith("russian_edition_source_problem=") for line in lines)
    report = _doctor(tmp_path / "config.json", output_dir)
    assert "russian_edition" not in report.facts
    assert "Поиск русских изданий" not in render_russian(report)


# ── The config reaches the pass ──────────────────────────────────────────


def _config_file(tmp_path: Path, **keys: Any) -> Path:
    path = tmp_path / "weekly.json"
    path.write_text(json.dumps({"sources": [], **keys}), encoding="utf-8")
    return path


ROUTES = {"Why We Sleep": "why-we-sleep", "Matthew Walker": "why-we-sleep", "Уолкер": "why-we-sleep"}


def _two_books(tmp_path: Path) -> tuple[Path, Path]:
    return workspace(
        tmp_path,
        [
            book("c-walker", WALKER_TITLE, WALKER, first_year=2017),
            book("c-other", "Some Other Book", "Nobody Known", first_year=2020),
        ],
    )


def _complete(tmp_path: Path, fetcher: FakeFetcher, clock: FakeClock | None = None, **keys: Any) -> dict[str, Any]:
    state_dir, output_dir = _two_books(tmp_path)
    stage = _stage(fetcher, clock=clock)
    stage.set_config_path(_config_file(tmp_path, **{SWITCH: True, **keys}))
    return stage.complete(output_dir, state_dir, effective_date=TODAY)


def test_the_stage_takes_every_undecided_book_and_the_pass_defaults_without_limits(tmp_path):
    state_dir, output_dir = workspace(
        tmp_path,
        [
            book("c-walker", WALKER_TITLE, WALKER, first_year=2017),
            book("c-decided", "Decided Book", "Some Author", editor_status="reject"),
        ],
        current_keys=[],
    )
    stage = _stage(FakeFetcher(routed(ROUTES)))

    result = stage.complete(output_dir, state_dir, effective_date=TODAY)

    assert result["scope"] == "undecided"
    assert result["enabled_sources"] == [SOURCE_LIVELIB, SOURCE_WIKIDATA, SOURCE_RSL, SOURCE_LITRES]
    assert by_id(state_dir)["c-walker"]["russian_edition"]["status"] == "confirmed"
    assert "russian_edition" not in by_id(state_dir)["c-decided"]


def test_the_book_ceiling_reaches_the_pass(tmp_path):
    result = _complete(tmp_path, FakeFetcher(routed(ROUTES)), russian_edition_max_books=1)

    assert result["over_ceiling"] == 1


def test_the_livelib_ceiling_reaches_the_pass(tmp_path):
    fetcher = FakeFetcher(routed(ROUTES))

    result = _complete(tmp_path, fetcher, russian_edition_max_livelib_searches=1)

    assert len(fetcher.asked(SOURCE_LIVELIB)) == 1
    assert result["sources"][SOURCE_LIVELIB]["not_asked"] >= 1


def test_the_skipped_sources_reach_the_pass(tmp_path):
    fetcher = FakeFetcher(routed(ROUTES))

    result = _complete(tmp_path, fetcher, russian_edition_skip_sources=[SOURCE_LIVELIB, SOURCE_LITRES])

    assert result["enabled_sources"] == [SOURCE_WIKIDATA, SOURCE_RSL]
    assert fetcher.asked(SOURCE_LIVELIB) == [] and fetcher.asked(SOURCE_LITRES) == []
    assert fetcher.asked(SOURCE_RSL)


def test_the_budget_reaches_the_pass(tmp_path):
    clock = FakeClock()
    fetcher = FakeFetcher(routed(ROUTES), clock=clock, step=50.0)

    result = _complete(tmp_path, fetcher, clock=clock, russian_edition_budget_seconds=60)

    assert result["budget_exhausted"] >= 1


@pytest.mark.parametrize(
    ("keys", "message"),
    [
        ({SWITCH: "yes"}, "'russian_edition_completion' must be true or false"),
        ({"russian_edition_max_books": -1}, "'russian_edition_max_books' must be a whole number, 0 or more"),
        ({"russian_edition_max_books": True}, "'russian_edition_max_books' must be a whole number, 0 or more"),
        (
            {"russian_edition_max_livelib_searches": 2.5},
            "'russian_edition_max_livelib_searches' must be a whole number, 0 or more",
        ),
        ({"russian_edition_budget_seconds": 0}, "'russian_edition_budget_seconds' must be a number of seconds above 0"),
        (
            {"russian_edition_budget_seconds": "60"},
            "'russian_edition_budget_seconds' must be a number of seconds above 0",
        ),
        ({"russian_edition_skip_sources": ["ozon"]}, "'russian_edition_skip_sources' must be a list of source names"),
        ({"russian_edition_skip_sources": "livelib"}, "'russian_edition_skip_sources' must be a list of source names"),
        ({"russian_edition_skip_sources": [SOURCE_RSL, SOURCE_LITRES]}, "must leave rsl or litres to ask"),
    ],
)
def test_an_unusable_value_is_a_config_error(keys, message):
    errors = validate_config({"sources": [], **keys})

    assert len(errors) == 1 and errors[0].startswith("top-level config: ") and message in errors[0]


def test_usable_values_are_no_config_error():
    config = {
        "sources": [],
        SWITCH: True,
        "russian_edition_max_books": 40,
        "russian_edition_max_livelib_searches": 0,
        "russian_edition_budget_seconds": 900.5,
        "russian_edition_skip_sources": [SOURCE_LIVELIB, SOURCE_WIKIDATA, SOURCE_LITRES],
    }

    assert validate_config(config) == []


def test_an_unusable_value_met_by_the_weekly_run_fails_the_stage_and_not_the_run(tmp_path, capsys):
    fetcher = FakeFetcher(recorded("why-we-sleep"))
    adapters = deterministic_adapters()
    adapters.russian_edition_completion = _stage(fetcher)

    output_dir, result = _run_weekly(
        tmp_path, adapters, **{SWITCH: True, "russian_edition_skip_sources": [SOURCE_RSL, SOURCE_LITRES]}
    )

    (entry,) = _entries(result)
    assert entry["status"] == "failed"
    assert entry["detail"] == {"status": "failed", "reason": "invalid_weekly_config"}
    assert fetcher.requests == []
    assert result.outcome.value.startswith("completed")
    assert (output_dir / "weekly-review.html").exists()
    run_health(output_dir)
    assert "russian_edition_completion_problem=invalid_weekly_config" in capsys.readouterr().out.splitlines()


# ── The heartbeat ────────────────────────────────────────────────────────


def test_the_heartbeat_counts_books_checked_and_editions_found(tmp_path):
    state_dir, output_dir = _two_books(tmp_path)
    clock = FakeClock()
    presented: list[dict[str, Any]] = []
    stage = _stage(FakeFetcher(routed(ROUTES), clock=clock, step=40.0), TitleMatcher(clock=clock), clock=clock)
    stage.set_snapshot_progress_presentation(presented.append)

    stage.complete(output_dir, state_dir, effective_date=TODAY)

    lines = [snapshot_progress_line(snapshot) for snapshot in presented]
    assert lines[0] == "Поиск русских изданий: проверено 0, найдено 0."
    assert "Поиск русских изданий: проверено 2, найдено 1." in lines
    # No title, author or catalogue text reaches the heartbeat.
    assert not any(WALKER_TITLE in line or WALKER in line or RUSSIAN_TITLE in line for line in lines)
    # The heartbeat is throttled like the other stages': at most one line a minute.
    assert len(lines) <= 1 + int(clock.now // 60)


def test_the_progress_callback_changes_nothing_the_pass_does(tmp_path):
    results = []
    for name, progress in (("without", None), ("with", lambda tried, found: None)):
        (tmp_path / name).mkdir()
        state_dir, output_dir = _two_books(tmp_path / name)
        clock = FakeClock()
        results.append(
            russian_edition_completion.run_russian_edition_completion(
                output_dir,
                state_dir,
                effective_date=TODAY,
                fetcher=FakeFetcher(routed(ROUTES)),
                matcher=TitleMatcher(),
                monotonic=clock,
                sleeper=clock.sleep,
                progress=progress,
            )
        )

    assert results[0] == results[1]


# ── Wiring ───────────────────────────────────────────────────────────────


def test_the_binding_keeps_an_injected_stage_and_binds_the_production_one_otherwise():
    injected = _stage(FakeFetcher({}))
    adapters = deterministic_adapters()
    adapters.russian_edition_completion = injected
    bind_russian_edition_completion(adapters)
    fresh = deterministic_adapters()
    bind_russian_edition_completion(fresh)

    assert adapters.russian_edition_completion is injected
    assert isinstance(fresh.russian_edition_completion, ProductionRussianEditionCompletionStage)


def test_the_health_reader_agrees_with_the_pass_on_its_stage_name_and_failure_limit():
    assert (HEALTH_STAGE_NAME, HEALTH_FAILURE_LIMIT) == (STAGE_NAME, SOURCE_FAILURE_LIMIT)


# ── Fix round 1 ──────────────────────────────────────────────────────────


class _KirkusNeverCalled:
    """A bound Kirkus pass: the run asks whether one is due, which reads the config."""

    def collect(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("no Kirkus source is configured")


def test_a_config_without_a_catalogue_does_not_stop_the_run_through_another_config_reader(tmp_path):
    fetcher = FakeFetcher(recorded("why-we-sleep"))
    adapters = deterministic_adapters()
    adapters.russian_edition_completion = _stage(fetcher)
    # The weekly run asks kirkus_is_due(), which reads the config through load_validated_config().
    adapters.kirkus_precollection = _KirkusNeverCalled()

    output_dir, result = _run_weekly(
        tmp_path, adapters, **{SWITCH: True, "russian_edition_skip_sources": [SOURCE_RSL, SOURCE_LITRES]}
    )

    assert result.outcome.value.startswith("completed")
    assert (output_dir / "weekly-review.html").exists()
    (entry,) = _entries(result)
    assert entry["detail"] == {"status": "failed", "reason": "invalid_weekly_config"}
    assert fetcher.requests == []
    # The explicit validation still names the value.
    assert validate_config(json.loads((tmp_path / "config.json").read_text("utf-8")))


# The report transaction is held only around the write.


def _watched_transactions(monkeypatch) -> list[tuple[str, str]]:
    """Record every report transaction's purpose as it opens and closes."""
    events: list[tuple[str, str]] = []
    original = WeeklyReportStore.transaction

    @contextlib.contextmanager
    def watched(self, purpose: str = "feedback"):
        with original(self, purpose):
            events.append(("open", purpose))
            try:
                yield
            finally:
                events.append(("close", purpose))

    monkeypatch.setattr(WeeklyReportStore, "transaction", watched)
    return events


def _open_transactions(events: list[tuple[str, str]]) -> list[str]:
    held: list[str] = []
    for kind, purpose in events:
        if kind == "open":
            held.append(purpose)
        else:
            held.remove(purpose)
    return held


def test_no_report_transaction_is_held_while_the_sources_are_asked(tmp_path, monkeypatch):
    events = _watched_transactions(monkeypatch)
    output_dir = tmp_path / "run"
    seen: list[tuple[bool, list[str]]] = []
    answers = recorded("why-we-sleep")

    def answer(source: str, target: str) -> Any:
        seen.append((WeeklyReportStore(output_dir).transaction_in_progress(), _open_transactions(events)))
        return answers(source, target)

    adapters = deterministic_adapters()
    adapters.russian_edition_completion = _stage(FakeFetcher(answer))

    _output_dir, result = _run_weekly(tmp_path, adapters, **{SWITCH: True})

    assert seen and all(not marker and not held for marker, held in seen)
    # The write still happens under the stage's own report transaction.
    assert ("open", STAGE_NAME) in events
    (entry,) = _entries(result)
    assert entry["detail"]["confirmed"] == 1
    (recorded_book,) = [
        record for record in read_run_output(output_dir, "candidate_history") if record.get("title") == WALKER_TITLE
    ]
    assert recorded_book["russian_edition"]["status"] == "confirmed"
    (current,) = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
    assert current["russian_edition"]["status"] == "confirmed"


def test_a_fetcher_that_raises_leaves_no_transaction_marker_and_the_run_completes(tmp_path):
    def raising(source: str, target: str) -> Any:
        raise RuntimeError("connection reset")

    adapters = deterministic_adapters()
    adapters.russian_edition_completion = _stage(FakeFetcher(raising))

    output_dir, result = _run_weekly(tmp_path, adapters, **{SWITCH: True})

    (entry,) = _entries(result)
    assert entry["status"] == "failed"
    assert not (output_dir / TRANSACTION_MARKER).exists()
    assert not WeeklyReportStore(output_dir).transaction_in_progress()
    assert result.outcome.value.startswith("completed")
    assert (output_dir / "weekly-review.html").exists()


def test_a_run_interrupted_during_the_lookups_leaves_no_transaction_marker(tmp_path):
    def interrupted(source: str, target: str) -> Any:
        raise KeyboardInterrupt

    adapters = deterministic_adapters()
    adapters.russian_edition_completion = _stage(FakeFetcher(interrupted))

    with pytest.raises(KeyboardInterrupt):
        _run_weekly(tmp_path, adapters, **{SWITCH: True})

    # The stage had written nothing: the review stays readable and the next attempt can open a transaction.
    assert not (tmp_path / "run" / TRANSACTION_MARKER).exists()
    assert not WeeklyReportStore(tmp_path / "run").transaction_in_progress()


# The snapshot stays fresh during a long model call.


class _SlowMatcher(TitleMatcher):
    """A model call longer than the doctor's stale threshold, which returns once the snapshot was refreshed."""

    def __init__(self, clock: FakeClock, snapshot: Path, presented: list[dict[str, Any]]) -> None:
        super().__init__()
        self.clock = clock
        self.snapshot = snapshot
        self.presented = presented
        self.refreshed_during_call = False
        self.presented_during_call = 0

    def match(self, items: Any, *, timeout: float) -> Any:
        before = len(self.presented)
        started = self.clock.now
        self.clock.now += snapshot_stale_after_seconds() + 20.0
        real_deadline = time.monotonic() + 5.0
        while time.monotonic() < real_deadline:
            payload = json.loads(self.snapshot.read_text(encoding="utf-8"))
            if payload["status"] == "running" and payload["elapsed_seconds"] >= self.clock.now - started - 1:
                self.refreshed_during_call = True
                break
            time.sleep(0.05)
        # Many more keepalive slices pass at the same moment of the run's clock.
        time.sleep(1.0)
        self.presented_during_call = len(self.presented) - before
        return super().match(items, timeout=timeout)


def test_the_snapshot_is_refreshed_during_a_long_model_call_without_more_heartbeat_lines(tmp_path):
    state_dir, output_dir = _two_books(tmp_path)
    clock = FakeClock()
    presented: list[dict[str, Any]] = []
    matcher = _SlowMatcher(clock, output_dir / "progress-snapshot.json", presented)
    stage = _stage(FakeFetcher(routed(ROUTES)), matcher, clock=clock)
    stage.set_snapshot_progress_presentation(presented.append)

    result = stage.complete(output_dir, state_dir, effective_date=TODAY)

    assert result["confirmed"] == 1
    assert matcher.refreshed_during_call
    # One heartbeat line at most for a call of three minutes and more, however often the snapshot is refreshed.
    assert matcher.presented_during_call <= 1
    assert len(presented) <= 1 + int(clock.now // 60)


def test_the_kept_alive_matcher_returns_and_raises_what_the_matcher_does():
    calls: list[str] = []

    class Raising:
        def match(self, items: Any, *, timeout: float) -> Any:
            raise TimeoutError("the model's own timeout")

    kept = russian_edition_completion._KeptAliveMatcher(FakeMatcher(result={"c": 1}), lambda: calls.append("tick"))
    assert kept.match([], timeout=1.0) == {"c": 1}
    with pytest.raises(TimeoutError):
        russian_edition_completion._KeptAliveMatcher(Raising(), lambda: None).match([], timeout=1.0)


# The stage takes only the time left in the run.


class _ClockSettingStage:
    """The work-age stage, which leaves the run's clock where a test wants it."""

    def __init__(self, clock: FakeClock, now: float) -> None:
        self.clock = clock
        self.now = now

    def complete(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.clock.now = self.now
        return {"status": "ok"}


class _RecordingStage:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def complete(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.kwargs = kwargs
        return {"status": "ok"}


def _elapsed_leaving(seconds: float) -> float:
    """The elapsed time of the run's current start at which the stage has ``seconds`` left."""
    return run_time_limit.russian_edition_seconds_left(0.0) - seconds


def _run_weekly_at(tmp_path: Path, stage: Any, elapsed: float) -> tuple[Path, Any]:
    clock = FakeClock()
    adapters = deterministic_adapters()
    adapters.work_age_completion = _ClockSettingStage(clock, elapsed)
    adapters.russian_edition_completion = stage
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = _config(tmp_path, **{SWITCH: True})
    output_dir = tmp_path / "run"
    output_dir.mkdir(exist_ok=True)
    _write_capture(tmp_path)
    adapters.capture.prepare(config_path, output_dir, live=False)
    _write_extraction(output_dir)
    return output_dir, run_weekly_workflow(config_path, output_dir, adapters=adapters, monotonic=clock)


def test_the_stage_budget_is_capped_at_the_time_left_in_the_run(tmp_path):
    stage = _RecordingStage()

    _output_dir, result = _run_weekly_at(tmp_path, stage, _elapsed_leaving(600.0))

    assert stage.kwargs["budget_seconds"] == pytest.approx(600.0)
    assert result.outcome.value.startswith("completed")


def test_the_production_stage_takes_the_smaller_of_its_configured_budget_and_the_time_left(tmp_path):
    clock = FakeClock()
    fetcher = FakeFetcher(routed(ROUTES), clock=clock, step=50.0)
    state_dir, output_dir = _two_books(tmp_path)
    stage = _stage(fetcher, clock=clock)
    stage.set_config_path(_config_file(tmp_path, **{SWITCH: True}))

    result = stage.complete(output_dir, state_dir, effective_date=TODAY, budget_seconds=60.0)

    assert result["budget_exhausted"] >= 1
    assert json.loads((output_dir / "progress-snapshot.json").read_text("utf-8"))["stage_budget_seconds"] == 60.0


def test_with_too_little_time_left_the_stage_is_skipped_and_named_and_the_run_goes_on(tmp_path, capsys):
    fetcher = FakeFetcher(recorded("why-we-sleep"))
    left = russian_edition_completion.MIN_STAGE_SECONDS - 1.0

    output_dir, result = _run_weekly_at(tmp_path, _stage(fetcher), _elapsed_leaving(left))

    assert fetcher.requests == []
    (entry,) = _entries(result)
    assert entry["status"] == "skipped"
    assert entry["detail"]["reason"] == "run_time_limit"
    assert result.outcome.value.startswith("completed")
    assert (output_dir / "weekly-review.html").exists()
    run_health(output_dir)
    lines = capsys.readouterr().out.splitlines()
    assert "russian_edition_completion=skipped" in lines
    assert "russian_edition_completion_skipped=run_time_limit" in lines
    assert "пропущен: до предела времени ночного запуска" in render_russian(
        _doctor(tmp_path / "config.json", output_dir)
    )


def test_the_stage_never_adds_to_the_run_worst_case():
    from test_issue_1162_git_time_limits import DEPLOY
    from test_issue_1162_git_time_limits import _timeout_start_seconds

    worst = run_time_limit.worst_case_seconds()
    # 11 hours 28 minutes, under the scheduled units' limit.
    assert worst == 11 * 3600 + 28 * 60
    assert worst < _timeout_start_seconds(DEPLOY / "autoscout-run.service")
    # A start whose gate, Git commands and earlier stages all took their full limits leaves the stage nothing.
    earlier_stages = run_time_limit.stage_budgets_seconds() - editorial_fit_shadow.DEFAULT_STAGE_BUDGET_SECONDS
    assert run_time_limit.russian_edition_seconds_left(run_time_limit.start_seconds() + earlier_stages) == 0.0
    assert run_time_limit.russian_edition_seconds_left(0.0) > russian_edition_completion.STAGE_BUDGET_SECONDS


# Diagnostics come from the latest attempt only.


def test_an_earlier_attempt_of_the_same_date_is_not_reported_for_a_later_one(tmp_path, capsys):
    fetcher = FakeFetcher(dict.fromkeys((SOURCE_LIVELIB, SOURCE_WIKIDATA, SOURCE_RSL, SOURCE_LITRES), BLOCKED))
    adapters = deterministic_adapters()
    adapters.russian_edition_completion = _stage(fetcher)
    output_dir, _first = _run_weekly(tmp_path / "first", adapters, **{SWITCH: True})
    # A later attempt of the same date with the pass switched off appends its entries to the same log.
    retry = deterministic_adapters()
    retry.russian_edition_completion = _stage(FakeFetcher({}))
    retry_dir, second = _run_weekly(tmp_path / "retry", retry, **{SWITCH: False})
    assert _entries(second) == []
    log_path = output_dir / "workflow-log.json"
    first_log = json.loads(log_path.read_text(encoding="utf-8"))
    retry_log = json.loads((retry_dir / "workflow-log.json").read_text(encoding="utf-8"))
    assert "operation-start" in [entry.get("stage") for entry in retry_log]
    log_path.write_text(json.dumps(first_log + retry_log), encoding="utf-8")

    # The earlier attempt's entry is still in the log, and is not this attempt's.
    assert [entry for entry in first_log if entry.get("stage") == STAGE_NAME]
    capsys.readouterr()
    run_health(output_dir)
    assert not any(line.startswith("russian_edition_") for line in capsys.readouterr().out.splitlines())
    report = _doctor(tmp_path / "first" / "config.json", output_dir)
    assert "russian_edition" not in report.facts
    assert "Поиск русских изданий" not in render_russian(report)


# ── Fix round 2 ──────────────────────────────────────────────────────────


# A failed write of the stage's report file is contained.


def test_a_failed_write_of_the_report_file_leaves_no_marker_and_the_run_completes(tmp_path, monkeypatch):
    from autoscout import learned_field_completion

    original_write = learned_field_completion.write_json

    def failing_report_write(path: Path, data: Any) -> None:
        if Path(path).name == russian_edition_completion.REPORT_FILENAME:
            raise OSError("disk full")
        original_write(path, data)

    monkeypatch.setattr(learned_field_completion, "write_json", failing_report_write)
    adapters = deterministic_adapters()
    adapters.russian_edition_completion = _stage(FakeFetcher(recorded("why-we-sleep")))

    output_dir, result = _run_weekly(tmp_path, adapters, **{SWITCH: True})

    assert not (output_dir / TRANSACTION_MARKER).exists()
    assert not WeeklyReportStore(output_dir).transaction_in_progress()
    assert result.outcome.value.startswith("completed")
    assert "Есть русское издание" in (output_dir / "weekly-review.html").read_text(encoding="utf-8")
    assert not (output_dir / russian_edition_completion.REPORT_FILENAME).exists()
    (entry,) = _entries(result)
    assert entry["status"] == "ok"
    assert entry["detail"]["confirmed"] == 1
    assert entry["detail"]["report_file"] == "not_written"
    logged = json.loads((output_dir / "workflow-log.json").read_text(encoding="utf-8"))
    assert [item["detail"]["report_file"] for item in logged if item.get("stage") == STAGE_NAME] == ["not_written"]
    (recorded_book,) = [
        record for record in read_run_output(output_dir, "candidate_history") if record.get("title") == WALKER_TITLE
    ]
    assert recorded_book["russian_edition"]["status"] == "confirmed"


def test_the_report_file_is_written_after_the_report_transaction_closes(tmp_path, monkeypatch):
    from autoscout import learned_field_completion

    original_write = learned_field_completion.write_json
    seen: list[bool] = []

    def watched_write(path: Path, data: Any) -> None:
        if Path(path).name == russian_edition_completion.REPORT_FILENAME:
            seen.append(WeeklyReportStore(Path(path).parent).transaction_in_progress())
        original_write(path, data)

    monkeypatch.setattr(learned_field_completion, "write_json", watched_write)
    adapters = deterministic_adapters()
    adapters.russian_edition_completion = _stage(FakeFetcher(recorded("why-we-sleep")))

    output_dir, _result = _run_weekly(tmp_path, adapters, **{SWITCH: True})

    assert seen == [False]
    report = json.loads((output_dir / russian_edition_completion.REPORT_FILENAME).read_text(encoding="utf-8"))
    assert report["confirmed"] == 1 and "report_file" not in report


# No value of a russian_edition_* key makes the validation raise.

_HUGE = json.loads("1" + "0" * 400)
_ODD_VALUES = [
    _HUGE,
    -_HUGE,
    json.loads("1e999"),
    json.loads("-Infinity"),
    json.loads("NaN"),
    [[1]],
    [[SOURCE_RSL]],
    {"a": 1},
    None,
    True,
    False,
    "60",
    [],
    [None],
    [{"x": 1}],
]


@pytest.mark.parametrize(
    "key",
    [
        "russian_edition_max_books",
        "russian_edition_max_livelib_searches",
        "russian_edition_budget_seconds",
        "russian_edition_skip_sources",
        SWITCH,
    ],
)
@pytest.mark.parametrize("value", _ODD_VALUES, ids=lambda value: type(value).__name__ + ":" + repr(value)[:12])
def test_no_value_of_a_key_makes_the_validation_raise(key, value):
    from autoscout.russian_edition_settings import config_errors
    from autoscout.russian_edition_settings import weekly_settings

    errors = config_errors({key: value})
    validate_config({"sources": [], key: value})
    if errors:
        with pytest.raises(ValueError):
            weekly_settings({key: value})
    else:
        weekly_settings({key: value})


@pytest.mark.parametrize("value", [_HUGE, json.loads("1e999"), json.loads("-Infinity"), json.loads("NaN")])
def test_a_budget_no_float_can_hold_is_a_config_error(value):
    errors = validate_config({"sources": [], "russian_edition_budget_seconds": value})

    assert len(errors) == 1 and "'russian_edition_budget_seconds' must be a number of seconds above 0" in errors[0]


def test_a_huge_budget_met_by_the_weekly_run_fails_the_stage_and_not_the_run(tmp_path):
    fetcher = FakeFetcher(recorded("why-we-sleep"))
    adapters = deterministic_adapters()
    adapters.russian_edition_completion = _stage(fetcher)
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = _config(tmp_path, **{SWITCH: True})
    # Python's json writes an integer of any size, as an operator could type one.
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["russian_edition_budget_seconds"] = _HUGE
    config_path.write_text(json.dumps(config), encoding="utf-8")
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    _write_capture(tmp_path)
    adapters.capture.prepare(config_path, output_dir, live=False)
    _write_extraction(output_dir)

    result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

    (entry,) = _entries(result)
    assert entry["detail"] == {"status": "failed", "reason": "invalid_weekly_config"}
    assert fetcher.requests == []
    assert result.outcome.value.startswith("completed")


# A retry that failed before its operation-start is the latest attempt.


def test_a_retry_that_failed_at_the_preflight_does_not_report_the_earlier_attempt(tmp_path, capsys):
    fetcher = FakeFetcher(dict.fromkeys((SOURCE_LIVELIB, SOURCE_WIKIDATA, SOURCE_RSL, SOURCE_LITRES), BLOCKED))
    adapters = deterministic_adapters()
    adapters.russian_edition_completion = _stage(fetcher)
    output_dir, _first = _run_weekly(tmp_path, adapters, **{SWITCH: True})
    # The earlier attempt names its blocked sources.
    run_health(output_dir)
    assert "russian_edition_source_problem=livelib|blocked=1|failed=0|not_asked=0" in capsys.readouterr().out

    # The log a same-operation retry leaves when its repository preflight fails: the earlier
    # attempt's entries without their terminal entry, then the failure and a new terminal entry
    # (weekly_workflow._bad_outcome), and no operation-start.
    log_path = output_dir / "workflow-log.json"
    earlier = json.loads(log_path.read_text(encoding="utf-8"))
    operation_id = next(str(entry["operation_id"]) for entry in earlier if entry.get("operation_id"))
    kept = [entry for entry in earlier if entry.get("stage") not in {"terminal", "outcome-candidate"}]
    assert any(entry.get("stage") == STAGE_NAME for entry in kept)
    retry_entries = [
        {"stage": "repository-preflight", "status": "failed", "reason": "dirty_worktree", "at": "2026-07-14T03:00:00Z"},
        {"stage": "terminal", "status": "intervention_required", "at": "2026-07-14T03:00:01Z"},
    ]
    log_path.write_text(
        json.dumps(kept + [{**entry, "operation_id": operation_id} for entry in retry_entries]), encoding="utf-8"
    )

    run_health(output_dir)
    assert not any(line.startswith("russian_edition_") for line in capsys.readouterr().out.splitlines())
    report = _doctor(tmp_path / "config.json", output_dir)
    assert "russian_edition" not in report.facts
    assert "Поиск русских изданий" not in render_russian(report)


@pytest.mark.parametrize("later_stage", ["repository-preflight", "repository-update", "code-gate", "operation-start"])
def test_every_pre_start_stage_of_a_later_attempt_is_an_attempt_boundary(tmp_path, later_stage):
    from autoscout.russian_edition_health import last_outcome

    blocked = {"stage": STAGE_NAME, "status": "ok", "detail": {"sources": {SOURCE_LIVELIB: {"blocked": 1}}}}
    (tmp_path / "workflow-log.json").write_text(
        json.dumps(
            [
                {"stage": "operation-start", "status": "ok"},
                blocked,
                {"stage": later_stage, "status": "failed", "reason": "x"},
                {"stage": "terminal", "status": "intervention_required"},
            ]
        ),
        encoding="utf-8",
    )
    assert last_outcome(tmp_path) is None
    (tmp_path / "workflow-log.json").write_text(
        json.dumps(
            [{"stage": "operation-start", "status": "ok"}, blocked, {"stage": "terminal", "status": "completed"}]
        ),
        encoding="utf-8",
    )
    outcome = last_outcome(tmp_path)
    assert outcome is not None and outcome.sources


def test_the_health_reader_knows_the_workflow_code_gate_stage():
    from autoscout.russian_edition_health import ATTEMPT_BOUNDARY_STAGES
    from autoscout.weekly_workflow import CODE_GATE_STAGE

    assert CODE_GATE_STAGE in ATTEMPT_BOUNDARY_STAGES


# A malformed switch is named.


@pytest.mark.parametrize("value", ["true", 1, None, [True]])
def test_a_switch_that_is_not_a_boolean_keeps_the_lookups_off_and_is_named(tmp_path, capsys, value):
    fetcher = FakeFetcher(recorded("why-we-sleep"))
    adapters = deterministic_adapters()
    adapters.russian_edition_completion = _stage(fetcher)

    output_dir, result = _run_weekly(tmp_path, adapters, **{SWITCH: value})

    assert fetcher.requests == []
    (entry,) = _entries(result)
    assert entry["status"] == "failed"
    assert entry["detail"] == {"status": "failed", "reason": "invalid_weekly_config"}
    assert result.outcome.value.startswith("completed")
    assert "russian_edition" not in json.loads((output_dir / "scouting-candidates.json").read_text("utf-8"))[0]
    run_health(output_dir)
    lines = capsys.readouterr().out.splitlines()
    assert "russian_edition_completion=failed" in lines
    assert "russian_edition_completion_problem=invalid_weekly_config" in lines
    report = _doctor(tmp_path / "config.json", output_dir)
    assert "Поиск русских изданий в последнем поиске не удался (invalid_weekly_config)" in render_russian(report)


# ── Fix round 3 ──────────────────────────────────────────────────────────


def _write_log(directory: Path, log: Any) -> None:
    (directory / "workflow-log.json").write_text(json.dumps(log), encoding="utf-8")


def _completed_run(tmp_path: Path, capsys: Any) -> Path:
    """The output of a completed run without the stage, whose log a test then replaces."""
    output_dir, result = _run_weekly(tmp_path, deterministic_adapters())
    assert result.outcome.value.startswith("completed")
    capsys.readouterr()
    return output_dir


def _diagnostics(tmp_path: Path, output_dir: Path, capsys: Any) -> tuple[list[str], list[str]]:
    """The stage's run-health lines and doctor lines for the log in *output_dir*."""
    run_health(output_dir)
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("russian_edition_")]
    text = render_russian(_doctor(tmp_path / "config.json", output_dir))
    return lines, [line for line in text.splitlines() if "Поиск русских изданий" in line]


# A workflow log of any shape is read without raising.

_BLOCKED_STAGE = {"stage": STAGE_NAME, "status": "ok", "detail": {"sources": {SOURCE_LIVELIB: {"blocked": 1}}}}
_BLOCKED_LINE = "russian_edition_source_problem=livelib|blocked=1|failed=0|not_asked=0"


def test_the_diagnostics_read_the_stage_from_a_completed_run_log(tmp_path, capsys):
    output_dir = _completed_run(tmp_path, capsys)
    _write_log(output_dir, [{"stage": "operation-start"}, _BLOCKED_STAGE, {"stage": "terminal"}])

    lines, text = _diagnostics(tmp_path, output_dir, capsys)

    assert _BLOCKED_LINE in lines
    assert text


@pytest.mark.parametrize(
    "log",
    [
        {"stage": STAGE_NAME},
        "not a log",
        42,
        None,
        [1, "x", None, [STAGE_NAME], {"stage": ["a"]}, {"stage": {"a": 1}}, {"stage": None}, {"stage": 3}],
    ],
)
def test_a_log_or_entries_the_reader_cannot_read_give_no_russian_edition_lines(tmp_path, capsys, log):
    from autoscout.russian_edition_health import last_outcome

    output_dir = _completed_run(tmp_path, capsys)
    _write_log(output_dir, log)

    assert last_outcome(output_dir) is None
    assert _diagnostics(tmp_path, output_dir, capsys) == ([], [])


@pytest.mark.parametrize(
    "entry",
    [
        {"stage": STAGE_NAME, "status": ["failed"], "detail": ["x"]},
        {"stage": STAGE_NAME, "status": {"a": 1}, "detail": {"sources": ["livelib"], "reason": ["x"]}},
        {"stage": STAGE_NAME, "status": "failed", "detail": {"sources": {SOURCE_LIVELIB: ["blocked"]}}},
        {
            "stage": STAGE_NAME,
            "status": "ok",
            "detail": {
                "confirmed": "7",
                "exception_type": 3,
                "sources": {SOURCE_LIVELIB: {"blocked": "2", "failed": 1.5, "not_asked": None}, "other": 1},
            },
        },
    ],
)
def test_a_stage_entry_of_any_shape_is_read_without_raising(tmp_path, capsys, entry):
    output_dir = _completed_run(tmp_path, capsys)
    _write_log(output_dir, [{"stage": "operation-start"}, entry, {"stage": "terminal"}])

    lines, _text = _diagnostics(tmp_path, output_dir, capsys)

    assert lines and lines[0].startswith("russian_edition_completion=")
    assert not any(line.startswith("russian_edition_source_problem=") for line in lines)


@pytest.mark.parametrize("stage", [["operation-start"], {"stage": STAGE_NAME}, None, 7])
def test_an_entry_with_a_stage_that_is_not_a_string_is_skipped(tmp_path, capsys, stage):
    output_dir = _completed_run(tmp_path, capsys)
    _write_log(output_dir, [{"stage": "operation-start"}, _BLOCKED_STAGE, {"stage": stage, "status": "failed"}])

    lines, text = _diagnostics(tmp_path, output_dir, capsys)

    assert _BLOCKED_LINE in lines
    assert any("LiveLib — отказов 1" in line for line in text)


def test_a_status_that_is_not_a_plain_word_is_named_unknown(tmp_path, capsys):
    output_dir = _completed_run(tmp_path, capsys)
    _write_log(output_dir, [{"stage": STAGE_NAME, "status": ["failed"]}])

    lines, _text = _diagnostics(tmp_path, output_dir, capsys)

    assert lines == ["russian_edition_completion=unknown", "russian_edition_completion_problem=unknown"]


def test_a_log_that_is_not_json_gives_no_outcome(tmp_path):
    from autoscout.russian_edition_health import last_outcome

    (tmp_path / "workflow-log.json").write_text("{not json", encoding="utf-8")

    assert last_outcome(tmp_path) is None


# A retry that crashed before any of its stages is the latest attempt.


def test_a_retry_that_crashed_before_its_first_stage_does_not_report_the_earlier_attempt(tmp_path, capsys):
    from autoscout.russian_edition_health import last_outcome
    from autoscout.weekly_workflow import record_unexpected_error

    fetcher = FakeFetcher(dict.fromkeys((SOURCE_LIVELIB, SOURCE_WIKIDATA, SOURCE_RSL, SOURCE_LITRES), BLOCKED))
    adapters = deterministic_adapters()
    adapters.russian_edition_completion = _stage(fetcher)
    output_dir, _first = _run_weekly(tmp_path, adapters, **{SWITCH: True})
    assert last_outcome(output_dir) is not None
    # A same-operation retry whose repository preflight raised: the workflow logs only this entry.
    record_unexpected_error(
        output_dir, tmp_path / "config.json", date.fromisoformat(RUN_DATE), RuntimeError("boom"), "weekly-run"
    )
    logged = json.loads((output_dir / "workflow-log.json").read_text(encoding="utf-8"))
    assert [entry.get("stage") for entry in logged[-2:]] == ["terminal", "unexpected-error"]

    assert last_outcome(output_dir) is None
    capsys.readouterr()
    run_health(output_dir)
    assert not any(line.startswith("russian_edition_") for line in capsys.readouterr().out.splitlines())
    report = _doctor(tmp_path / "config.json", output_dir)
    assert "russian_edition" not in report.facts
    assert "Поиск русских изданий" not in render_russian(report)


def test_an_attempt_that_crashed_after_the_stage_still_reports_it(tmp_path):
    from autoscout.russian_edition_health import last_outcome

    crash = {"stage": "unexpected-error", "status": "failed", "detail": {"exception_type": "RuntimeError"}}
    _write_log(tmp_path, [{"stage": "operation-start"}, _BLOCKED_STAGE, {"stage": "review-render"}, crash])
    outcome = last_outcome(tmp_path)
    assert outcome is not None and outcome.sources

    # Another attempt that crashed before its first stage follows a crashed attempt too.
    _write_log(tmp_path, [{"stage": "operation-start"}, _BLOCKED_STAGE, crash, crash])
    assert last_outcome(tmp_path) is None


def test_the_health_reader_knows_the_workflow_unexpected_error_stage():
    from autoscout.russian_edition_health import ATTEMPT_END_STAGES
    from autoscout.unexpected_error import UNEXPECTED_ERROR_STAGE

    assert UNEXPECTED_ERROR_STAGE in ATTEMPT_END_STAGES


# The standalone command says when its report file was not written.


def test_the_command_says_when_the_report_file_was_not_written(tmp_path, capsys, monkeypatch):
    from autoscout import learned_field_completion
    from autoscout.__main__ import main

    state_dir, output_dir = workspace(tmp_path, [book("c-walker", WALKER_TITLE, WALKER, first_year=2017)])
    monkeypatch.setattr("autoscout.cli.common.weekly_run_in_progress", lambda state_dir: False)
    monkeypatch.setattr(
        "autoscout.russian_edition_completion.LiveSourceFetcher", lambda: FakeFetcher(recorded("why-we-sleep"))
    )
    monkeypatch.setattr("autoscout.russian_edition_completion.CodexSameWorkMatcher", TitleMatcher)
    monkeypatch.setattr("autoscout.russian_edition_completion.time.sleep", lambda seconds: None)
    original_write = learned_field_completion.write_json

    def failing_report_write(path: Path, data: Any) -> None:
        if Path(path).name == russian_edition_completion.REPORT_FILENAME:
            raise OSError("disk full")
        original_write(path, data)

    monkeypatch.setattr(learned_field_completion, "write_json", failing_report_write)
    arguments = ["complete-russian-editions", "--state-dir", str(state_dir), "--output-dir", str(output_dir)]

    code = main([*arguments, "--run-date", TODAY, "--skip-source", "wikidata", "--apply"])

    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    assert "warning=report_file_not_written" in lines
    assert "Русские издания сохранены, но файл отчёта о поиске не записан." in lines
    assert "confirmed=1" in " ".join(lines)
    assert not (output_dir / russian_edition_completion.REPORT_FILENAME).exists()


def test_the_command_says_nothing_about_a_report_file_that_was_written(tmp_path, capsys, monkeypatch):
    from autoscout.__main__ import main

    state_dir, output_dir = workspace(tmp_path, [book("c-walker", WALKER_TITLE, WALKER, first_year=2017)])
    monkeypatch.setattr("autoscout.cli.common.weekly_run_in_progress", lambda state_dir: False)
    monkeypatch.setattr(
        "autoscout.russian_edition_completion.LiveSourceFetcher", lambda: FakeFetcher(recorded("why-we-sleep"))
    )
    monkeypatch.setattr("autoscout.russian_edition_completion.CodexSameWorkMatcher", TitleMatcher)
    monkeypatch.setattr("autoscout.russian_edition_completion.time.sleep", lambda seconds: None)
    arguments = ["complete-russian-editions", "--state-dir", str(state_dir), "--output-dir", str(output_dir)]

    assert main([*arguments, "--run-date", TODAY, "--skip-source", "wikidata", "--apply"]) == 0

    assert "report_file_not_written" not in capsys.readouterr().out
    assert (output_dir / russian_edition_completion.REPORT_FILENAME).exists()


# The doctor states only what the log shows about a failed stage.


def test_the_doctor_line_for_a_failed_stage_makes_no_claim_about_the_review():
    from autoscout.russian_edition_health import StageOutcome
    from autoscout.russian_edition_health import doctor_line

    assert doctor_line(StageOutcome(status="failed", failure="RuntimeError")) == (
        "Поиск русских изданий в последнем поиске не удался (RuntimeError)."
    )
    assert doctor_line(StageOutcome(status="failed")) == "Поиск русских изданий в последнем поиске не удался."
    for outcome in (StageOutcome(status="failed"), StageOutcome(status="skipped", failure="run_time_limit")):
        assert "обзор" not in doctor_line(outcome)
