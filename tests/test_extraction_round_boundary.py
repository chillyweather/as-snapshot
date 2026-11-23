"""Deterministic seams for the bounded extraction round, the progress lease and the runtime budget."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import pytest

from autoscout.codex_recovery import AttemptDiagnosis
from autoscout.codex_recovery import CodexProgressLease
from autoscout.codex_recovery import ExtractionAttempt
from autoscout.codex_recovery import ExtractionStatus
from autoscout.codex_recovery import ProductionCodexAdapter
from autoscout.codex_recovery import build_codex_extraction_prompt
from autoscout.codex_recovery import validate_extraction_accounting
from autoscout.codex_workflow import finalize_codex_run
from autoscout.extraction_round import execute_bounded_extraction_round
from autoscout.runtime_budget import CODEX_ABSOLUTE_CEILING_SECONDS
from autoscout.runtime_budget import CODEX_NO_PROGRESS_TIMEOUT_SECONDS
from autoscout.runtime_budget import FINALIZATION_RESERVE_SECONDS
from autoscout.runtime_budget import SEGMENT_BUDGET_SECONDS
from autoscout.runtime_budget import WEEKLY_OPERATION_CEILING_SECONDS


def test_progress_lease_recognizes_only_structured_execution_events() -> None:
    lease = CodexProgressLease()
    lease.feed(
        b"arbitrary output\n"
        b"{not json}\n"
        b'{"type":"unknown","payload":{"type":"reasoning"}}\n'
        b'{"type":"response_item","payload":{"type":"reasoning","content":"discard"}}\n'
        b'{"type":"event_msg","payload":{"type":"token_count","content":"discard"}}\n'
        b'{"type":"agent_message","content":"discard"}\n'
        b'{"type":"custom_tool_call_output","content":"discard"}\n'
        b'{"type":"message","content":"model prose"}\n',
        10.0,
    )

    assert lease.last_progress_at == 10.0
    assert lease.event_counts == {"reasoning": 1, "token_count": 1}
    assert lease.idle_seconds(25.0) == 15.0
    assert "discard" not in repr(lease.__dict__)


def test_progress_lease_does_not_renew_for_unrecognized_text() -> None:
    lease = CodexProgressLease()
    lease.feed(b'text only\nnot json\n{"type":"unknown"}\n', 5.0)

    assert lease.last_progress_at is None
    assert lease.idle_seconds(185.0) is None


def test_segment_gate_classifies_queued_work_as_unfinished() -> None:
    items = [{"source_name": "one"}, {"source_name": "two"}]
    boundary_calls: list[tuple[str, str]] = []

    class FakeClock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

        def advance(self, seconds: float) -> None:
            self.value += seconds

    clock = FakeClock()

    def run(context):
        clock.advance(0.03)
        return {"status": "done"}

    def boundary(context, stage, threshold, supplier):
        boundary_calls.append((context.item["source_name"], supplier))
        return {"status": "boundary"}

    completions = execute_bounded_extraction_round(
        items,
        worker_count=1,
        per_attempt_ceiling=10.0,
        portfolio_deadline=1.0,
        admission_deadline=0.01,
        clock=clock,
        run=run,
        boundary=boundary,
    )

    assert len(completions) == 2
    assert boundary_calls == [("two", "segment_deadline")]


def test_prompts_use_only_the_retained_capture_without_model_overrides() -> None:
    variants = [
        {},
        {"source_metadata": {"metadata_exact_book_list": True, "signal_family": "award"}},
        {
            "source_name": "BookMarks (LitHub) - New Nonfiction",
            "source_url": "https://bookmarks.reviews/category/non-fiction/",
        },
    ]
    for item in variants:
        prompt = build_codex_extraction_prompt(item)
        assert "retained capture.json is the complete and only input" in prompt
        assert "Do not read a memory folder" in prompt
        assert "--model" not in prompt
        assert "profile" not in prompt.casefold()
        assert "reasoning level" not in prompt.casefold()


@pytest.mark.parametrize("terminal_class", ["unfinished", "portfolio_capacity_stop", "execution_safety_stop"])
def test_finalize_fails_closed_for_unfinished_outcomes_before_writing_replay_config(
    tmp_path: Path, terminal_class: str
) -> None:
    capture = tmp_path / "capture.json"
    capture.write_text("{}", encoding="utf-8")
    worklist = {
        "work_items": [
            {
                "source_name": "Source",
                "source_family": "award",
                "source_url": "https://example.test/source",
                "capture_file": capture.name,
                "expected_extraction_file": "result.json",
                "source_metadata": {},
            }
        ],
        "precollected_sources": [],
    }
    (tmp_path / "codex-extraction-worklist.json").write_text(json.dumps(worklist), encoding="utf-8")
    outcomes = {"outcomes": [{"source_name": "Source", "outcome": "absent", "terminal_class": terminal_class}]}
    (tmp_path / "codex-extraction-outcomes.json").write_text(json.dumps(outcomes), encoding="utf-8")
    replay = tmp_path / "codex-replay-config.json"
    replay.write_text("sentinel", encoding="utf-8")

    assert finalize_codex_run(tmp_path) == 1
    assert replay.read_text(encoding="utf-8") == "sentinel"


def test_zero_terminal_result_segment_stops_without_continuation(tmp_path: Path) -> None:
    class Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

        def advance(self, seconds: float) -> None:
            self.value += seconds

    clock = Clock()
    items = []
    for index in range(2):
        capture = tmp_path / f"capture-{index}.json"
        capture.write_text("{}", encoding="utf-8")
        items.append(
            {
                "source_name": f"Source {index}",
                "source_url": f"https://example.test/{index}",
                "capture_file": capture.name,
                "expected_extraction_file": f"result-{index}.json",
                "operation_id": "operation",
                "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
            }
        )
    (tmp_path / "codex-extraction-worklist.json").write_text(json.dumps({"work_items": items}), encoding="utf-8")

    class NoTerminalProgressAgent:
        calls = 0

        def begin_round(self, source_names: list[str]) -> None:
            clock.advance(1800.0)

        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            self.calls += 1
            raise AssertionError("queued work must not invoke the agent")

    agent = NoTerminalProgressAgent()
    result = ProductionCodexAdapter(
        agent,
        monotonic=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        runtime_budget_seconds=3780.0,
        per_attempt_timeout_seconds=1800.0,
        max_attempts=1,
        max_workers=2,
    ).fulfill(tmp_path)

    assert agent.calls == 0
    assert result["status"] == "unfinished"
    assert result["segment_count"] == 1
    assert result["unfinished_items"] == 2


def test_runtime_policy_keeps_one_weekly_ceiling_and_finalization_reserve() -> None:
    assert SEGMENT_BUDGET_SECONDS == CODEX_ABSOLUTE_CEILING_SECONDS == 1800
    assert CODEX_NO_PROGRESS_TIMEOUT_SECONDS == 180
    assert WEEKLY_OPERATION_CEILING_SECONDS == 7200
    assert FINALIZATION_RESERVE_SECONDS == 600


def test_timeout_diagnostics_are_terminal_and_not_retryable() -> None:
    attempt = ExtractionAttempt(
        ExtractionStatus.TIMEOUT,
        False,
        diagnostic="codex_no_progress_timeout",
        # The six diagnosis values moved onto AttemptDiagnosis, which is the
        # one type that carries them (Issue #449). The assertions below are
        # unchanged in meaning: same values, one accessor deeper.
        diagnosis=AttemptDiagnosis(
            progress_event_counts={"reasoning": 1},
            no_progress_quiet_seconds=180.0,
        ),
    )

    assert attempt.retryable is False
    assert attempt.diagnosis.progress_event_counts == {"reasoning": 1}
    assert attempt.diagnosis.no_progress_quiet_seconds == 180.0
    assert json.dumps(attempt.diagnosis.progress_event_counts) == '{"reasoning": 1}'


@pytest.mark.parametrize(
    "counts",
    [
        {"ready_items": 4, "failed_items": 0, "skipped_items": 0, "unfinished_items": 0},
        {"ready_items": 0, "failed_items": 4, "skipped_items": 0, "unfinished_items": 0},
        {"ready_items": 0, "failed_items": 0, "skipped_items": 0, "unfinished_items": 4},
        {"ready_items": 0, "failed_items": 0, "skipped_items": 4, "unfinished_items": 0},
    ],
)
def test_production_accounting_invariant_covers_all_terminal_classes(counts: dict[str, int]) -> None:
    result = {**counts, "total_items": 4, "execution_safety_stop_count": 1}
    validate_extraction_accounting(result, 4)


def test_production_accounting_invariant_rejects_missing_or_double_counted_items() -> None:
    with pytest.raises(ValueError, match="accounting invariant"):
        validate_extraction_accounting(
            {
                "ready_items": 0,
                "failed_items": 0,
                "skipped_items": 0,
                "unfinished_items": 0,
                "total_items": 1,
            },
            1,
        )
    with pytest.raises(ValueError, match="accounting invariant"):
        validate_extraction_accounting(
            {
                "ready_items": 0,
                "failed_items": 0,
                "skipped_items": 0,
                "unfinished_items": 1,
                "total_items": 1,
                "execution_safety_stop_count": 1,
            },
            2,
        )


@pytest.mark.parametrize("callback_result", ["none", "raise"])
def test_round_boundary_callback_always_produces_one_diagnosed_completion(callback_result: str) -> None:
    def boundary(*_args):
        if callback_result == "raise":
            raise RuntimeError("injected boundary programming error")
        return None

    completions = execute_bounded_extraction_round(
        [{}],
        worker_count=1,
        per_attempt_ceiling=10.0,
        portfolio_deadline=0.0,
        clock=lambda: 0.0,
        run=lambda _context: {"status": "unreachable"},
        boundary=boundary,
    )

    assert len(completions) == 1
    assert completions[0].result is not None
    assert completions[0].result["diagnostics"][0]["status"] == "worker_exception"


@pytest.mark.parametrize("callback_result", ["none", "raise"])
def test_round_exception_callback_always_produces_one_diagnosed_completion(callback_result: str) -> None:
    def on_exception(*_args):
        if callback_result == "raise":
            raise RuntimeError("injected exception callback programming error")
        return None

    completions = execute_bounded_extraction_round(
        [{}],
        worker_count=1,
        per_attempt_ceiling=10.0,
        portfolio_deadline=1.0,
        clock=lambda: 0.0,
        run=lambda _context: (_ for _ in ()).throw(RuntimeError("worker failed")),
        boundary=lambda *_args: {"status": "boundary"},
        on_exception=on_exception,
    )

    assert len(completions) == 1
    assert completions[0].result is not None
    assert completions[0].result["diagnostics"][0]["status"] == "worker_exception"


def test_progress_lease_classifies_split_jsonl_line() -> None:
    lease = CodexProgressLease()
    lease.feed(b'{"type":"response_item","payload":{"type":"reason', 10.0)
    assert lease.last_progress_at is None
    lease.feed(b'ing"}}\n', 11.0)

    assert lease.last_progress_at == 11.0
    assert lease.event_counts == {"reasoning": 1}


def test_duplicate_boundary_claim_yields_one_completion_and_never_calls_the_supplier() -> None:
    """Cover the duplicate boundary claim guard in ``_boundary_result``.

    The guard is defensive: only the collector thread claims a boundary today,
    so a second claim is not reachable through ordinary scheduling.  It is
    exercised here by marking a pending context as already claimed through the
    ``on_poll`` seam, which hands out the live contexts.  Nothing sleeps and
    nothing depends on wall-clock ordering, so this cannot become the next
    flaky test.
    """

    class FakeClock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

        def advance(self, seconds: float) -> None:
            self.value += seconds

    clock = FakeClock()
    boundary_calls: list[str] = []

    def run(context):
        # Push past the admission deadline so the queued item cannot start and
        # returns a boundary request to the collector instead.
        clock.advance(0.05)
        return {"status": "done"}

    def boundary(context, stage, threshold, supplier):
        boundary_calls.append(str(context.item.get("source_name", "")))
        return {"status": "boundary"}

    def on_poll(contexts, _now):
        for context in contexts:
            context.boundary_claimed = True

    completions = execute_bounded_extraction_round(
        [{"source_name": "one"}, {"source_name": "two"}],
        worker_count=1,
        per_attempt_ceiling=10.0,
        portfolio_deadline=1.0,
        admission_deadline=0.01,
        clock=clock,
        run=run,
        boundary=boundary,
        on_poll=on_poll,
    )

    # Every work item still receives exactly one terminal result.
    assert len(completions) == 2
    assert len({completion.context.submission_sequence for completion in completions}) == 2

    duplicates = [
        completion
        for completion in completions
        if completion.result is not None and completion.result.get("terminal_class") == "duplicate_boundary_claim"
    ]
    assert len(duplicates) == 1
    duplicate = duplicates[0]
    assert duplicate.result["diagnostics"][0]["status"] == "duplicate_boundary_claim"
    assert duplicate.context.item["source_name"] == "two"

    # The losing claim must short-circuit before the real boundary supplier.
    assert "two" not in boundary_calls


# The portfolio tests below drive the real ProductionCodexAdapter.fulfill with a
# deterministic fake agent.  They replace the simulation that the issue #411
# reproduction generator used to run, with the same 28-source portfolio, the
# same three-worker waves and the same production budget.

_CAPTURE_LINK = "https://example.test/book-0"
_CAPTURE_HTML = f'<html><body><a href="{_CAPTURE_LINK}">Book 0</a></body></html>'
_PORTFOLIO_SIZE = 28
_PRODUCTION_BUDGET_SECONDS = 6600.0


class _WaveClock:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = 0.0

    def __call__(self) -> float:
        with self._lock:
            return self._value

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._value += float(seconds)


class _WaveAgent:
    """Three-worker waves: every wave costs its slowest member, capped by the offered timeout."""

    def __init__(self, clock: _WaveClock, seconds: dict[str, float], invalid: frozenset[str] = frozenset()) -> None:
        self._clock = clock
        self._seconds = seconds
        self._invalid = invalid
        self.call_counts: dict[str, int] = {name: 0 for name in seconds}
        self._condition = threading.Condition()
        self._waves: list[list[str]] = []
        self._wave_index = 0
        self._barrier: threading.Barrier | None = None
        self._members: list[str] = []

    def begin_round(self, source_names: list[str]) -> None:
        with self._condition:
            self._waves = [source_names[start : start + 3] for start in range(0, len(source_names), 3)]
            self._wave_index = 0
            self._barrier = None
            self._members = []
            self._condition.notify_all()

    def extract(self, item, capture_path, artifact_path):
        raise AssertionError("deadline-aware execution expected")

    def _finish_wave(self, offered: float) -> None:
        longest = max(self._seconds[name] for name in self._members)
        self._clock.advance(min(longest, offered))
        with self._condition:
            self._wave_index += 1
            self._barrier = None
            self._members = []
            self._condition.notify_all()

    def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
        name = str(item["source_name"])
        with self._condition:
            self.call_counts[name] += 1
            while self._wave_index < len(self._waves) and name not in self._waves[self._wave_index]:
                self._condition.wait()
            if self._barrier is None:
                self._barrier = threading.Barrier(
                    len(self._waves[self._wave_index]), action=lambda: self._finish_wave(timeout_seconds)
                )
            barrier = self._barrier
            self._members.append(name)
        barrier.wait()
        if self._seconds[name] >= timeout_seconds - 1e-9:
            return ExtractionAttempt(ExtractionStatus.TIMEOUT, False, diagnostic="codex_absolute_ceiling")
        link = "https://example.test/not-in-capture" if name in self._invalid else _CAPTURE_LINK
        artifact_path.write_text(
            json.dumps({**_artifact_identity(item), "entries": [_entry(name, link)]}), encoding="utf-8"
        )
        return ExtractionAttempt(ExtractionStatus.PRODUCED, name not in self._invalid, 0)


def _artifact_identity(item: dict) -> dict:
    return {key: item[key] for key in ("source_name", "source_url", "operation_id", "capture_sha256")}


def _entry(name: str, link: str) -> dict:
    return {
        "title": f"{name} Book 1",
        "primary_authors": ["Shim Author"],
        "source_url": link,
        "evidence_url": link,
        "evidence_note": "Shim evidence note.",
        "candidate_category": "popular science",
        "signal_type": "award shortlist",
        "signal_strength": "shortlist",
    }


def _fulfill_portfolio(
    tmp_path: Path,
    seconds_for,
    *,
    invalid: frozenset[str] = frozenset(),
    already_validated: frozenset[str] = frozenset(),
) -> tuple[dict, _WaveAgent]:
    names = [f"Source {index:02d}" for index in range(_PORTFOLIO_SIZE)]
    items = []
    for index, name in enumerate(names):
        capture = tmp_path / f"capture-{index:02d}.json"
        capture.write_text(
            json.dumps(
                {"source_name": name, "source_url": f"https://example.test/source-{index:02d}", "html": _CAPTURE_HTML}
            ),
            encoding="utf-8",
        )
        item = {
            "source_name": name,
            "source_family": "media",
            "source_url": f"https://example.test/source-{index:02d}",
            "capture_file": capture.name,
            "expected_extraction_file": f"result-{index:02d}.json",
            "operation_id": "operation",
            "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
            "source_metadata": {},
        }
        items.append(item)
        if name in already_validated:
            (tmp_path / item["expected_extraction_file"]).write_text(
                json.dumps({**_artifact_identity(item), "entries": [_entry(name, _CAPTURE_LINK)]}), encoding="utf-8"
            )
    (tmp_path / "codex-extraction-worklist.json").write_text(json.dumps({"work_items": items}), encoding="utf-8")
    clock = _WaveClock()
    agent = _WaveAgent(clock, {name: seconds_for(name) for name in names}, invalid)
    result = ProductionCodexAdapter(
        agent,
        monotonic=clock,
        sleeper=clock.advance,
        runtime_budget_seconds=_PRODUCTION_BUDGET_SECONDS,
        per_attempt_timeout_seconds=float(SEGMENT_BUDGET_SECONDS),
        max_attempts=2,
        max_workers=3,
    ).fulfill(tmp_path)
    return result, agent


def test_a_portfolio_beyond_the_weekly_budget_stops_at_the_boundary_and_names_what_is_unfinished(
    tmp_path: Path,
) -> None:
    result, _agent = _fulfill_portfolio(tmp_path, lambda _name: 900.0)

    assert result["status"] == "unfinished"
    assert result["ready_items"] == 18
    assert result["unfinished_items"] == 10
    assert result["capacity_stop_count"] == 10
    assert result["segment_count"] == 3
    assert result["failed_items"] == 0
    assert result["skipped_items"] == 0
    assert result["extraction_budget_seconds"] == _PRODUCTION_BUDGET_SECONDS
    assert result["duration_seconds"] <= _PRODUCTION_BUDGET_SECONDS
    assert result["duration_seconds"] + FINALIZATION_RESERVE_SECONDS <= WEEKLY_OPERATION_CEILING_SECONDS
    assert [timing["submission_sequence"] for timing in result["source_timings"]] == list(range(1, _PORTFOLIO_SIZE + 1))


def test_a_forced_continuation_keeps_portfolio_order_and_skips_an_already_validated_artifact(
    tmp_path: Path,
) -> None:
    result, agent = _fulfill_portfolio(tmp_path, lambda _name: 400.0, already_validated=frozenset({"Source 00"}))

    assert result["segment_count"] > 1
    assert [timing["submission_sequence"] for timing in result["source_timings"]] == list(range(1, _PORTFOLIO_SIZE + 1))
    assert all(segment["terminal_count"] > 0 for segment in result["segments"])
    assert result["segments"][-1]["unfinished_count"] == 0
    assert agent.call_counts["Source 00"] == 0
    assert max(agent.call_counts.values()) <= 1
    assert result["ready_items"] == _PORTFOLIO_SIZE


def test_an_invalid_extraction_is_terminal_and_named_without_stopping_the_portfolio(tmp_path: Path) -> None:
    last = f"Source {_PORTFOLIO_SIZE - 1:02d}"
    result, _agent = _fulfill_portfolio(
        tmp_path, lambda name: 142.0 if name == last else 400.0, invalid=frozenset({last})
    )

    assert result["ready_items"] == 27
    assert result["failed_items"] == 1
    assert result["unfinished_items"] == 0
    invalid = next(timing for timing in result["source_timings"] if timing["source_name"] == last)
    assert invalid["terminal"] == "invalid_output"


def test_slow_large_capture_sources_end_as_boundary_stops_inside_the_weekly_bound(tmp_path: Path) -> None:
    """The 2026-08-12 shape: five sources never finish; the rest are judged on their own result."""
    slow = {f"Source {index:02d}" for index in range(_PORTFOLIO_SIZE - 5, _PORTFOLIO_SIZE)}
    fast = {"Source 00": 112.0, "Source 01": 171.0, "Source 02": 142.0}

    result, _agent = _fulfill_portfolio(
        tmp_path,
        lambda name: float(SEGMENT_BUDGET_SECONDS) if name in slow else fast.get(name, 400.0),
        invalid=frozenset({"Source 02"}),
    )

    assert result["unfinished_items"] == 5
    assert result["execution_safety_stop_count"] == 4
    assert any(
        timing["terminal"] == "validated" and timing["duration_seconds"] == 400.0 for timing in result["source_timings"]
    )
    assert result["duration_seconds"] <= _PRODUCTION_BUDGET_SECONDS
    assert result["duration_seconds"] + FINALIZATION_RESERVE_SECONDS <= WEEKLY_OPERATION_CEILING_SECONDS
