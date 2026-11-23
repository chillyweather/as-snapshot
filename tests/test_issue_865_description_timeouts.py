"""Offline slow-call replay, deferred retries, and refreshed run caveats."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from adjudication_test_support import VALID_TEXT
from adjudication_test_support import citation
from adjudication_test_support import fetcher
from adjudication_test_support import page
from adjudication_test_support import record
from adjudication_test_support import response
from adjudication_test_support import workspace
from state_store_support import read_state
from state_store_support import state_snapshot

from autoscout.content_description import DESCRIPTION_FALLBACK
from autoscout.description_adjudication import SEARCH_DEPTH_FIELD
from autoscout.description_adjudication import CodexAdjudicationTimeoutError
from autoscout.description_adjudication import adjudicate_descriptions
from autoscout.description_diagnostics import refresh_description_diagnostics
from autoscout.generate_descriptions import _eligible
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import SynthesisResult
from autoscout.grounded_description_enrichment import enrich_grounded_descriptions
from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.pipeline import refresh_post_enrichment_artifacts
from autoscout.pipeline import run_pipeline
from autoscout.review_rendering import render_review
from autoscout.russian_workflow import completion_message

RUN_DATE = "2026-10-26"
URL = "https://publisher.example.test/work"


class Clock:
    def __init__(self) -> None:
        self.seconds = 0.0

    def __call__(self) -> float:
        return self.seconds

    def advance(self, seconds: float) -> None:
        self.seconds += seconds


class SlowAdapter:
    def __init__(self, clock: Clock, *, always_timeout: bool = False) -> None:
        self.clock = clock
        self.always_timeout = always_timeout
        self.calls: list[dict[str, Any]] = []

    def adjudicate(self, item: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(item))
        key = item["identity_key"]
        prior_calls = sum(call["identity_key"] == key for call in self.calls[:-1])
        if self.always_timeout or prior_calls == 0:
            self.clock.advance(240)
            raise CodexAdjudicationTimeoutError("timeout after 240s")
        self.clock.advance(10)
        return response(key, [citation(URL, "publisher.example.test")])


class Discovery:
    def discover(self, candidate: dict[str, Any], **_: Any) -> list[DiscoveryResult]:
        return [
            DiscoveryResult(
                URL,
                "publisher",
                "publisher_copy",
                "publisher-owner",
                "en",
                title=candidate["title"],
                primary_authors=tuple(candidate["primary_authors"]),
            )
        ]


class Fetch:
    def fetch(self, result: DiscoveryResult, **kwargs: Any) -> dict[str, Any]:
        return {
            "url": result.url,
            "source_kind": result.source_kind,
            "source_family": result.source_family,
            "owner_id": result.owner_id,
            "source_language": "en",
            "title": result.title,
            "primary_authors": list(result.primary_authors),
            "subject_text": "A book about memory, society and public life in a small town.",
            "display_text": f"{result.title} by {', '.join(result.primary_authors)}. A book about memory.",
            "checked_date": kwargs["checked_date"],
            "classification": "classified",
        }


class SlowSynthesizer:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.calls = 0

    def synthesize(self, *_: Any, **__: Any) -> SynthesisResult:
        self.calls += 1
        self.clock.advance(100)
        return SynthesisResult(VALID_TEXT)


def test_slow_replay_refreshes_latency_and_caveats_after_enrichment(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    write_json(
        config,
        {
            "run_date": RUN_DATE,
            "pilot_mode": True,
            "readiness_purpose": "recurring-weekly",
            "sources": [
                {
                    "name": "Slow Source",
                    "family": "media/curated list",
                    "url": "https://example.test/list",
                    "access_status": "open",
                    "extraction_method": "fixture",
                    "language": "en",
                    "fixture_candidates": [
                        {
                            "title": "Work One",
                            "primary_authors": ["Alex Author"],
                            "source_url": URL,
                            "candidate_category": "nonfiction",
                        }
                    ],
                }
            ],
        },
    )
    out, state = tmp_path / "run", tmp_path / "state"
    run_pipeline(config, out, state)
    assert "description_caveats" not in read_json(out / "run-summary.json")
    clock = Clock()
    synth = SlowSynthesizer(clock)
    stage = GroundedDescriptionStage(
        discovery=Discovery(), fetch=Fetch(), synthesizer=synth, monotonic=clock, max_workers=1
    )
    enriched = enrich_grounded_descriptions(stage, out, state, RUN_DATE, operation_id="slow-replay")
    grounded = read_json(out / "grounded-description-stage.json")
    expected = {"median_seconds": 100.0, "max_seconds": 100.0, "call_count": 1, "timeout_count": 1}
    assert grounded["call_latency"] == expected
    assert enriched["description_stage_report"]["call_latency"] == expected

    adapter = SlowAdapter(clock, always_timeout=True)
    adjudicated = adjudicate_descriptions(
        state,
        out,
        adapter=adapter,
        fetcher=fetcher({}),
        apply=True,
        effective_date=RUN_DATE,
        monotonic=clock,
        call_monotonic=clock,
        stage_budget_seconds=1000,
        max_workers=1,
    )
    adjudication = read_json(out / "description-adjudication-stage.json")
    assert adjudication["call_latency"] == {
        "median_seconds": 240.0,
        "max_seconds": 240.0,
        "call_count": 2,
        "timeout_count": 2,
    }
    assert adjudicated["description_adjudication_stage_report"]["call_latency"] == adjudication["call_latency"]
    assert len(adapter.calls) == 2
    assert refresh_post_enrichment_artifacts(config, out, state)["status"] == "ok"
    summary = read_json(out / "run-summary.json")
    assert summary["description_call_latency"] == {
        "synthesis": grounded["call_latency"],
        "adjudication": adjudication["call_latency"],
    }
    assert summary["description_caveats"] == [
        "Внимание: проверка описаний шла слишком медленно и не завершилась вовремя в 3 из 3 попыток.",
        "Внимание: Slow Source — без описания 1 из 1 новых книг.",
    ]
    assert set(summary["description_caveats"]) <= set(read_json(out / "pilot-status.json")["caveats"])
    render_review(out, state)
    for filename in ("weekly-review.md", "weekly-review.html"):
        text = (out / filename).read_text()
        assert all(caveat in text for caveat in summary["description_caveats"])
    message = completion_message("completed/attention-required", description_caveats=summary["description_caveats"])
    assert all(caveat in message for caveat in summary["description_caveats"])

    # Neither failed stage suppresses the next weekly attempt.
    enrich_grounded_descriptions(stage, out, state, "2026-10-27", operation_id="next-run")
    assert synth.calls == 2


def test_timeouts_retry_once_after_all_first_calls_and_success_applies(tmp_path: Path) -> None:
    keys = ["work one|alex author", "work two|alex author"]
    state, out = workspace(tmp_path, [record(f"book-{i}", key) for i, key in enumerate(keys)])
    clock = Clock()
    adapter = SlowAdapter(clock)
    result = adjudicate_descriptions(
        state,
        out,
        adapter=adapter,
        fetcher=fetcher({URL: page()}),
        apply=True,
        effective_date=RUN_DATE,
        monotonic=clock,
        call_monotonic=clock,
        stage_budget_seconds=1500,
        max_workers=1,
        deep_pass=True,
    )
    assert [call["identity_key"] for call in adapter.calls] == [*keys, *keys]
    assert adapter.calls[:2] == adapter.calls[2:]
    assert result["applied"] == 2
    assert result["rejected"] == 0
    assert result["description_adjudication_stage_report"]["call_latency"] == {
        "median_seconds": 125.0,
        "max_seconds": 240.0,
        "call_count": 4,
        "timeout_count": 2,
    }
    assert all(item["content_description"] == VALID_TEXT for item in read_state(state, "candidate_history"))


@pytest.mark.parametrize("budget,expected_calls", [(1000, 2), (470, 1)])
def test_repeated_timeouts_stop_after_one_retry_and_remain_eligible(
    tmp_path: Path, budget: int, expected_calls: int
) -> None:
    state, out = workspace(tmp_path, [record("book-1", "work one|alex author")])
    before = state_snapshot(state)
    clock = Clock()
    adapter = SlowAdapter(clock, always_timeout=True)
    for day in (RUN_DATE, "2026-10-27"):
        count_before = len(adapter.calls)
        adjudicate_descriptions(
            state,
            out,
            adapter=adapter,
            fetcher=fetcher({}),
            apply=True,
            effective_date=day,
            monotonic=clock,
            call_monotonic=clock,
            stage_budget_seconds=budget,
            max_workers=1,
            deep_pass=True,
        )
        assert len(adapter.calls) - count_before == expected_calls
        assert state_snapshot(state) == before


def test_deeper_timeout_replays_the_exact_deep_call(tmp_path: Path) -> None:
    state, out = workspace(tmp_path, [record("book-1", "work one|alex author")])
    clock = Clock()

    class DeepTimeoutAdapter(SlowAdapter):
        def adjudicate(self, item: dict[str, Any]) -> dict[str, Any]:
            self.calls.append(dict(item))
            if len(self.calls) == 1:
                self.clock.advance(5)
                return response(item["identity_key"], [], resolved=False, unresolved_reason="no evidence", text="")
            if len(self.calls) == 2:
                self.clock.advance(240)
                raise CodexAdjudicationTimeoutError("timeout after 240s")
            self.clock.advance(10)
            return response(item["identity_key"], [citation(URL, "publisher.example.test")])

    adapter = DeepTimeoutAdapter(clock)
    result = adjudicate_descriptions(
        state,
        out,
        adapter=adapter,
        fetcher=fetcher({URL: page()}),
        apply=True,
        effective_date=RUN_DATE,
        monotonic=clock,
        call_monotonic=clock,
        max_workers=1,
        deep_pass=True,
    )
    assert result["applied"] == 1
    assert len(adapter.calls) == 3
    assert adapter.calls[1] == adapter.calls[2]
    assert adapter.calls[1][SEARCH_DEPTH_FIELD] == "deep"
    report = result["description_adjudication_stage_report"]
    assert report["call_latency"] == {"median_seconds": 10.0, "max_seconds": 240.0, "call_count": 3, "timeout_count": 1}
    assert report["deep_pass"]["failed"] == 0


def test_thresholds_count_only_new_books_and_refresh_removes_old_caveats() -> None:
    def book(key: str, source: str, text: str, delta: str = "new") -> dict[str, Any]:
        return {
            "identity_key": key,
            "source_evidence": [{"source_name": source}],
            "content_description": text,
            "update_delta": delta,
        }

    books = [
        book("a", "Half", DESCRIPTION_FALLBACK),
        book("b", "Half", VALID_TEXT),
        book("c", "Majority", DESCRIPTION_FALLBACK),
        book("d", "Majority", VALID_TEXT, "unchanged"),
    ]
    books[2]["source_evidence"].extend([{"source_name": "Other"}, {"source_name": "Other"}])
    grounded = {
        "counts": {"attempted_no_description": 5},
        "call_latency": {"median_seconds": 10, "max_seconds": 10, "call_count": 5, "timeout_count": 1},
    }
    summary: dict[str, Any] = {}
    refresh_description_diagnostics(summary, books, grounded, {})
    assert summary["description_caveats"] == [
        "Внимание: Majority — без описания 1 из 1 новых книг.",
        "Внимание: Other — без описания 1 из 1 новых книг.",
    ]
    grounded["call_latency"]["timeout_count"] = 2  # type: ignore[index]
    refresh_description_diagnostics(summary, books, grounded, {})
    assert summary["description_caveats"][0].startswith("Внимание: проверка описаний шла слишком медленно")
    refresh_description_diagnostics(summary, [], {}, {})
    assert "description_caveats" not in summary
    assert "description_call_latency" not in summary


def test_budget_exhaustion_does_not_start_the_standalone_grounded_cooldown() -> None:
    candidate = record("book-1", "work one|alex author")
    candidate["grounded_description_attempts"] = [
        {"date": RUN_DATE, "outcome": "attempted_no_description", "reason_code": "candidate_budget_exhausted"}
    ]
    assert _eligible(candidate, effective_date=date(2026, 10, 27), retry_failed=False)


@pytest.mark.parametrize("old_date,old_operation", [("2026-10-25", "current"), (RUN_DATE, "previous")])
def test_previous_stage_metrics_are_not_reported_as_current(old_date: str, old_operation: str) -> None:
    summary: dict[str, Any] = {"run_date": RUN_DATE, "operation_id": "current"}
    old_stage = {
        "operation_date": old_date,
        "operation_id": old_operation,
        "counts": {"attempted_no_description": 1},
        "call_latency": {"median_seconds": 240, "max_seconds": 240, "call_count": 1, "timeout_count": 1},
    }
    refresh_description_diagnostics(summary, [], old_stage, old_stage)
    assert "description_caveats" not in summary
    assert "description_call_latency" not in summary


def test_timeout_retries_are_collected_through_the_heartbeat_tracker(tmp_path: Path) -> None:
    """A retry may run the full 240 s, so the tracker must keep polling through it."""

    class RecordingTracker:
        def __init__(self) -> None:
            self.collected: list[int] = []
            self.events: list[tuple[str, str]] = []

        def initial(self, *_: Any, **__: Any) -> None:
            self.events.append(("initial", ""))

        def begin(self, key: str) -> None:
            self.events.append(("begin", key))

        def finish(self, key: str) -> None:
            self.events.append(("finish", key))

        def final(self, **_: Any) -> None:
            self.events.append(("final", ""))

        def collect(self, futures: Any) -> list[Any]:
            self.collected.append(len(futures))
            return [future.result() for future in futures]

    key = "work one|alex author"
    state, out = workspace(tmp_path, [record("book-1", key)])
    clock = Clock()
    tracker = RecordingTracker()
    result = adjudicate_descriptions(
        state,
        out,
        adapter=SlowAdapter(clock),
        fetcher=fetcher({URL: page()}),
        apply=True,
        effective_date=RUN_DATE,
        monotonic=clock,
        call_monotonic=clock,
        stage_budget_seconds=1500,
        max_workers=1,
        snapshot_tracker=tracker,  # type: ignore[arg-type]
    )
    assert result["applied"] == 1
    # One collect for the first pass, one for the retry pass.
    assert tracker.collected == [1, 1]
    # The candidate stays running from its first call until the retry ends.
    assert [event for event in tracker.events if event[1] == key][-1] == ("finish", key)
    assert tracker.events[-1] == ("final", "")


def test_a_short_budget_retries_in_worklist_order_not_alphabetically(tmp_path: Path) -> None:
    """The one affordable retry goes to the candidate the first pass took first.

    A new book without text ranks ahead of an unsourced stub, so the worklist
    puts ``work two`` before the alphabetically earlier ``work one``.
    """
    keys = ["work one|alex author", "work two|alex author"]
    stub = record("book-0", keys[0], VALID_TEXT)
    stub["description_provenance"] = {
        "identity_key": keys[0],
        "content_description": VALID_TEXT,
        "provenance": "codex-generated",
    }
    state, out = workspace(tmp_path, [stub, record("book-1", keys[1])])
    clock = Clock()
    adapter = SlowAdapter(clock, always_timeout=True)
    adjudicate_descriptions(
        state,
        out,
        adapter=adapter,
        fetcher=fetcher({URL: page()}),
        apply=True,
        effective_date=RUN_DATE,
        monotonic=clock,
        call_monotonic=clock,
        stage_budget_seconds=800,
        max_workers=1,
    )
    first_pass = [call["identity_key"] for call in adapter.calls[:2]]
    assert first_pass == [keys[1], keys[0]]
    assert len(adapter.calls) == 3
    assert adapter.calls[2]["identity_key"] == first_pass[0]


def test_a_recovered_first_call_still_gets_the_deeper_search(tmp_path: Path) -> None:
    state, out = workspace(tmp_path, [record("book-1", "work one|alex author")])
    clock = Clock()

    class TimeoutThenDeclineAdapter(SlowAdapter):
        def adjudicate(self, item: dict[str, Any]) -> dict[str, Any]:
            self.calls.append(dict(item))
            if len(self.calls) == 1:
                self.clock.advance(240)
                raise CodexAdjudicationTimeoutError("timeout after 240s")
            if len(self.calls) == 2:
                self.clock.advance(5)
                return response(item["identity_key"], [], resolved=False, unresolved_reason="no evidence", text="")
            self.clock.advance(10)
            return response(item["identity_key"], [citation(URL, "publisher.example.test")])

    adapter = TimeoutThenDeclineAdapter(clock)
    result = adjudicate_descriptions(
        state,
        out,
        adapter=adapter,
        fetcher=fetcher({URL: page()}),
        apply=True,
        effective_date=RUN_DATE,
        monotonic=clock,
        call_monotonic=clock,
        max_workers=1,
        deep_pass=True,
    )
    assert len(adapter.calls) == 3
    assert SEARCH_DEPTH_FIELD not in adapter.calls[1]
    assert adapter.calls[2][SEARCH_DEPTH_FIELD] == "deep"
    assert result["applied"] == 1


def test_a_retried_candidate_stays_running_through_its_deeper_search(tmp_path: Path) -> None:
    key = "work one|alex author"
    state, out = workspace(tmp_path, [record("book-1", key)])
    clock = Clock()
    events: list[tuple[str, str]] = []

    class Tracker:
        def initial(self, *_: Any, **__: Any) -> None:
            pass

        def begin(self, item_key: str) -> None:
            events.append(("begin", item_key))

        def finish(self, item_key: str) -> None:
            events.append(("finish", item_key))

        def final(self, **_: Any) -> None:
            pass

        def collect(self, futures: Any) -> list[Any]:
            return [future.result() for future in futures]

    class TimeoutThenDeclineAdapter(SlowAdapter):
        def adjudicate(self, item: dict[str, Any]) -> dict[str, Any]:
            self.calls.append(dict(item))
            events.append(("call", item.get(SEARCH_DEPTH_FIELD, "first")))
            if len(self.calls) == 1:
                self.clock.advance(240)
                raise CodexAdjudicationTimeoutError("timeout after 240s")
            if len(self.calls) == 2:
                self.clock.advance(5)
                return response(item["identity_key"], [], resolved=False, unresolved_reason="no evidence", text="")
            self.clock.advance(10)
            return response(item["identity_key"], [citation(URL, "publisher.example.test")])

    adjudicate_descriptions(
        state,
        out,
        adapter=TimeoutThenDeclineAdapter(clock),
        fetcher=fetcher({URL: page()}),
        apply=True,
        effective_date=RUN_DATE,
        monotonic=clock,
        call_monotonic=clock,
        max_workers=1,
        deep_pass=True,
        snapshot_tracker=Tracker(),  # type: ignore[arg-type]
    )
    finishes = [index for index, event in enumerate(events) if event == ("finish", key)]
    deep_call = events.index(("call", "deep"))
    assert finishes and finishes[0] > deep_call
