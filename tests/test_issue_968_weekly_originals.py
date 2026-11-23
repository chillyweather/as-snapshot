"""The weekly run records original editions itself (issue #968).

Every test here is offline: the stages are doubles, or the production stage
is built with fake page-getter and proposer factories. No Codex CLI and no
network are reached.
"""

from __future__ import annotations

import copy
import json
import threading
from datetime import date
from pathlib import Path
from typing import Any

from state_store_support import read_state
from test_isbn_completion import _run_fixture_weekly
from test_issue_968_original_edition_completion import ATHILL_ID
from test_issue_968_original_edition_completion import ATHILL_KEY
from test_issue_968_original_edition_completion import CREDIT_PAGE
from test_issue_968_original_edition_completion import EVIDENCE_URL
from test_issue_968_original_edition_completion import OL_ENGLISH_DOCS
from test_issue_968_original_edition_completion import OL_PREFIX
from test_issue_968_original_edition_completion import TODAY
from test_issue_968_original_edition_completion import FakeClock
from test_issue_968_original_edition_completion import FakeGetter
from test_issue_968_original_edition_completion import FakeProposer
from test_issue_968_original_edition_completion import _history
from test_issue_968_original_edition_completion import _proposal
from test_issue_968_original_edition_completion import _read
from test_issue_968_original_edition_completion import _record
from test_issue_968_original_edition_completion import _workspace

from autoscout import pipeline
from autoscout import weekly_workflow
from autoscout.cli.weekly import run_mvp
from autoscout.editorial_taste_profile import save_taste_profile
from autoscout.original_edition import STATUS_CONFIRMED
from autoscout.original_edition_completion import STAGE_NAME
from autoscout.original_edition_completion import ProductionOriginalEditionCompletionStage
from autoscout.original_edition_completion import complete_originals
from autoscout.pipeline import refresh_post_enrichment_artifacts
from autoscout.russian_workflow import _SNAPSHOT_PROGRESS_STAGE_LABELS
from autoscout.russian_workflow import stage_message
from autoscout.weekly_workflow import STAGE_ORDER
from autoscout.weekly_workflow import bind_original_edition_completion
from autoscout.weekly_workflow import deterministic_adapters


class _RecordingOriginalStage:
    def __init__(self, events: list[str], error: Exception | None = None) -> None:
        self.events = events
        self.calls: list[tuple[Path, Path, str]] = []
        self.error = error

    def complete(
        self, output_dir: Path, state_dir: Path, *, effective_date: str, state_store: Any = None
    ) -> dict[str, Any]:
        self.calls.append((output_dir, state_dir, effective_date))
        self.events.append("original")
        if self.error is not None:
            raise self.error
        return {"status": "ok", "considered": 0, "records": [{"title": "secret"}]}


class _FakeProgress:
    def __init__(self) -> None:
        self.events: list[str] = []

    def initial(self, items: list[tuple[str, str]]) -> None:
        self.events.append("initial:" + ",".join(key for key, _title in items))

    def begin(self, key: str) -> None:
        self.events.append(f"begin:{key}")

    def finish(self, key: str) -> None:
        self.events.append(f"finish:{key}")

    def final(self, *, status: str = "finished") -> None:
        self.events.append("final")


def _stage_entries(result: Any) -> list[dict[str, Any]]:
    return [entry for entry in result.log if entry.get("stage") == STAGE_NAME]


def _order_recorders(monkeypatch: Any, events: list[str]) -> None:
    real_refresh = pipeline.refresh_post_enrichment_artifacts
    real_review = weekly_workflow._run_review_stage

    def refresh(*args: Any, **kwargs: Any) -> Any:
        events.append("refresh")
        return real_refresh(*args, **kwargs)

    def review(*args: Any, **kwargs: Any) -> Any:
        events.append("review")
        return real_review(*args, **kwargs)

    monkeypatch.setattr(pipeline, "refresh_post_enrichment_artifacts", refresh)
    monkeypatch.setattr(weekly_workflow, "_run_review_stage", review)


def test_weekly_run_calls_the_bound_stage_before_refresh_and_review(tmp_path, monkeypatch):
    events: list[str] = []
    _order_recorders(monkeypatch, events)
    adapters = deterministic_adapters()
    stage = _RecordingOriginalStage(events)
    adapters.original_edition_completion = stage

    output_dir, result = _run_fixture_weekly(tmp_path, adapters=adapters)

    assert events.count("original") == 1
    assert events.index("original") < events.index("refresh") < events.index("review")
    assert len(stage.calls) == 1
    called_output, called_state, called_date = stage.calls[0]
    assert called_output == output_dir
    assert called_state == output_dir
    assert date.fromisoformat(called_date)
    entries = _stage_entries(result)
    assert [entry["status"] for entry in entries] == ["ok"]
    assert "records" not in entries[0]["detail"]
    stages = [entry["stage"] for entry in result.log if entry.get("stage") in STAGE_ORDER]
    assert stages.index("enrichment") < stages.index(STAGE_NAME)


def test_a_raising_original_stage_never_stops_the_weekly_run(tmp_path):
    adapters = deterministic_adapters()
    adapters.original_edition_completion = _RecordingOriginalStage([], error=RuntimeError("boom"))

    output_dir, result = _run_fixture_weekly(tmp_path, adapters=adapters)

    entries = _stage_entries(result)
    assert [entry["status"] for entry in entries] == ["failed"]
    assert entries[0]["detail"] == {"status": "failed", "exception_type": "RuntimeError"}
    assert (output_dir / "weekly-review.html").exists()


def test_without_a_bound_stage_the_run_has_no_original_edition_step(tmp_path):
    _output_dir, result = _run_fixture_weekly(tmp_path)

    assert _stage_entries(result) == []


def test_the_binder_keeps_an_injected_stage_and_binds_the_production_one():
    adapters = deterministic_adapters()
    injected = ProductionOriginalEditionCompletionStage(getter_factory=lambda: None, proposer_factory=lambda: None)
    adapters.original_edition_completion = injected
    bind_original_edition_completion(adapters)
    assert adapters.original_edition_completion is injected

    fresh = deterministic_adapters()
    bind_original_edition_completion(fresh)
    assert isinstance(fresh.original_edition_completion, ProductionOriginalEditionCompletionStage)


def test_production_stage_writes_the_original_edition_and_reports_progress(tmp_path):
    state_dir, output_dir = _workspace(tmp_path, [_record()], [ATHILL_KEY])
    getter = FakeGetter({EVIDENCE_URL: CREDIT_PAGE, OL_PREFIX: json.dumps(OL_ENGLISH_DOCS)})
    stage = ProductionOriginalEditionCompletionStage(
        getter_factory=lambda: getter,
        proposer_factory=lambda: FakeProposer(_proposal()),
    )
    presented: list[dict[str, Any]] = []
    stage.set_snapshot_progress_presentation(presented.append)

    result = stage.complete(output_dir, state_dir, effective_date=TODAY)

    assert result["status"] == "ok"
    assert result["confirmed"] == 1
    assert _history(state_dir)[0]["original_edition"]["status"] == STATUS_CONFIRMED
    snapshot = json.loads((output_dir / "progress-snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["stage"] == STAGE_NAME
    assert snapshot["status"] == "finished"
    assert presented and all(isinstance(item, dict) for item in presented)


def test_production_stage_with_no_getter_or_proposer_answers_offline(tmp_path):
    state_dir, output_dir = _workspace(tmp_path, [_record()], [ATHILL_KEY])
    stage = ProductionOriginalEditionCompletionStage(getter_factory=lambda: None, proposer_factory=lambda: None)

    result = stage.complete(output_dir, state_dir, effective_date=TODAY)

    assert result["status"] == "ok"
    assert result["lookup_unavailable"] == 1
    assert "original_edition" not in _history(state_dir)[0]


def test_progress_follows_initial_begin_finish_and_final_per_record():
    records = [_record(), _record(key="second|author", candidate_id="candidate-002", title="Second")]
    progress = _FakeProgress()

    complete_originals(
        records,
        getter=FakeGetter(),
        proposer=FakeProposer({}),
        today=date.fromisoformat(TODAY),
        progress=progress,
    )

    assert progress.events == [
        f"initial:{ATHILL_ID},candidate-002",
        f"begin:{ATHILL_ID}",
        "begin:candidate-002",
        f"finish:{ATHILL_ID}",
        "finish:candidate-002",
        "final",
    ]


def test_progress_still_finishes_every_record_when_the_stage_has_no_getter():
    records = [_record(), _record(key="second|author", candidate_id="candidate-002", title="Second")]
    progress = _FakeProgress()

    report = complete_originals(records, getter=None, proposer=None, today=date.fromisoformat(TODAY), progress=progress)

    assert report.lookup_unavailable == 2
    assert progress.events[-1] == "final"
    assert [event for event in progress.events if event.startswith("finish:")] == [
        f"finish:{ATHILL_ID}",
        "finish:candidate-002",
    ]


def test_progress_walks_every_later_chunk_once_the_budget_has_stopped_the_pass():
    records = [_record(key=f"book {index}|author", candidate_id=f"candidate-{index:03d}") for index in range(11)]
    progress = _FakeProgress()

    report = complete_originals(
        records,
        getter=FakeGetter(),
        proposer=FakeProposer({}),
        today=date.fromisoformat(TODAY),
        budget_seconds=0,
        monotonic=FakeClock(),
        progress=progress,
    )

    assert report.budget_exhausted == 11
    begun = [event for event in progress.events if event.startswith("begin:")]
    finished = [event for event in progress.events if event.startswith("finish:")]
    assert begun == [f"begin:candidate-{index:03d}" for index in range(11)]
    assert finished == [f"finish:candidate-{index:03d}" for index in range(11)]
    assert progress.events[-1] == "final"


def test_the_russian_stage_message_and_snapshot_label_are_present():
    assert stage_message(STAGE_NAME) == "Ищу оригинальные издания переведённых книг..."
    assert _SNAPSHOT_PROGRESS_STAGE_LABELS[STAGE_NAME] == "Поиск оригинальных изданий"


def test_the_stage_order_places_originals_after_pm_logline_and_before_review():
    index = STAGE_ORDER.index(STAGE_NAME)
    assert STAGE_ORDER[index - 1] == "pm-logline-descriptions"
    # The work-age stage (#1138) follows, so a translation takes its original's age,
    # then the Russian-edition stage (#1181), which needs both.
    assert STAGE_ORDER[index + 1 :][:3] == ["work-age-completion", "russian-edition-completion", "review"]


# ── Placement: the confirmed English title reaches the refresh ───────────

RUN_DATE = "2026-11-07"
SPANISH_TITLE = "Ahora que se acerca el final"
ENGLISH_ORIGINAL = "Somewhere Towards the End"


def _spanish_book() -> dict[str, Any]:
    return {
        "title": SPANISH_TITLE,
        "primary_authors": ["Diana Athill"],
        "source_url": "https://example.test/books/ahora",
        "signal_strength": "major media list",
        "candidate_category": "history",
        "evidence_note": "Included in a curated list of new books.",
    }


def _english_rule() -> dict[str, Any]:
    return {
        "id": "taste-0001",
        "kind": "hard_rule",
        "scope": "topic",
        "subject": "Somewhere",
        "matching_terms": ["Somewhere"],
        "verbatim_quote": "Книги с английским названием Somewhere мы не берём.",
        "provenance": "placement fixture",
        "created_at": "2026-11-01",
        "created_by": "test",
        "status": "active",
    }


class _EveryItemProposer:
    def propose(self, items: Any, *, timeout: float) -> dict[str, dict[str, str]]:
        return {
            str(item["candidate_id"]): {"original_title": ENGLISH_ORIGINAL, "original_language": "en"} for item in items
        }


def test_a_confirmed_english_title_is_in_place_when_the_refresh_applies_hard_rules(tmp_path):
    config_path = tmp_path / "sources.json"
    source = {
        "name": "Fixture Spanish List",
        "family": "media/curated list",
        "url": "https://example.test/es-list",
        "access_status": "open",
        "extraction_method": "fixture",
        "check_frequency": "weekly",
        "language": "es",
        "fixture_candidates": [_spanish_book()],
    }
    config_path.write_text(json.dumps({"run_date": RUN_DATE, "sources": [source]}), encoding="utf-8")
    output_dir = tmp_path / "runs"
    state_dir = tmp_path / "state"
    save_taste_profile(state_dir, {"entries": [_english_rule()]})
    run_mvp(config_path, output_dir, state_dir)
    # Before the stage the English rule cannot see the English title.
    assert [record["title"] for record in _read(output_dir / "scouting-candidates.json")] == [SPANISH_TITLE]

    stage = ProductionOriginalEditionCompletionStage(
        getter_factory=lambda: FakeGetter({OL_PREFIX: json.dumps(OL_ENGLISH_DOCS)}),
        proposer_factory=_EveryItemProposer,
    )
    stage_result = stage.complete(output_dir, state_dir, effective_date=RUN_DATE)
    assert stage_result["confirmed"] == 1
    assert refresh_post_enrichment_artifacts(config_path, output_dir, state_dir)["status"] == "ok"

    assert _read(output_dir / "scouting-candidates.json") == []
    assert read_state(state_dir, "latest_scouting_candidates") == []
    (hidden,) = _read(output_dir / "hidden-by-rule.json")
    assert hidden["title"] == SPANISH_TITLE
    assert hidden["hidden_by_rule_id"] == "taste-0001"
    history = {row["title"]: row for row in read_state(state_dir, "candidate_history")}
    assert history[SPANISH_TITLE]["hidden_by_rule_id"] == "taste-0001"
    assert history[SPANISH_TITLE]["english_title"] == OL_ENGLISH_DOCS["docs"][0]["title"]
    assert history[SPANISH_TITLE]["original_edition"]["status"] == STATUS_CONFIRMED


# ── The heartbeat while a chunk is blocked ───────────────────────────────


class _GatedProposer(FakeProposer):
    """Blocks inside the proposal call until the gate opens, as a slow model would."""

    def __init__(self, gate: threading.Event, proposals: dict[str, dict[str, Any]]) -> None:
        super().__init__(proposals)
        self.gate = gate

    def propose(self, items: Any, *, timeout: float) -> Any:
        self.gate.wait(5)
        return super().propose(items, timeout=timeout)


class _CollectingProgress(_FakeProgress):
    """Polls the way CandidateProgressSnapshot.collect does, and records the polls."""

    def __init__(self, gate: threading.Event) -> None:
        super().__init__()
        self.gate = gate
        self.polls = 0

    def poll(self, *, now: float | None = None) -> None:
        self.polls += 1
        self.gate.set()

    def collect(self, futures: Any, *, tick_seconds: float = 0.25) -> list[Any]:
        results: list[Any] = []
        for future in futures:
            while not future.done():
                self.poll()
                try:
                    future.result(timeout=0.01)
                except TimeoutError:
                    pass
            results.append(future.result())
        return results


def test_a_blocked_chunk_keeps_polling_the_heartbeat_and_matches_the_direct_path():
    proposals = _proposal()
    records = [_record()]
    gate_direct = threading.Event()
    gate_direct.set()
    direct_records = copy.deepcopy(records)
    direct = complete_originals(
        direct_records,
        getter=FakeGetter({EVIDENCE_URL: CREDIT_PAGE, OL_PREFIX: json.dumps(OL_ENGLISH_DOCS)}),
        proposer=_GatedProposer(gate_direct, proposals),
        today=date.fromisoformat(TODAY),
    )

    gate = threading.Event()
    progress = _CollectingProgress(gate)
    polled_records = copy.deepcopy(records)
    polled = complete_originals(
        polled_records,
        getter=FakeGetter({EVIDENCE_URL: CREDIT_PAGE, OL_PREFIX: json.dumps(OL_ENGLISH_DOCS)}),
        proposer=_GatedProposer(gate, proposals),
        today=date.fromisoformat(TODAY),
        progress=progress,
    )

    assert progress.polls >= 1
    assert polled.as_dict() == direct.as_dict()
    assert polled.confirmed == 1
    assert polled_records[0]["original_edition"] == direct_records[0]["original_edition"]
