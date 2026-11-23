"""Issue #383 — the extraction stage reports progress instead of going silent.

On the 2026-08-05 live weekly run the ``codex`` stage printed one line and then
said nothing for 26.7 minutes, so the operating agent reported the healthy run
as stalled. Progress is now emitted as each source settles.
"""

from __future__ import annotations

import hashlib
import json
import time

from autoscout.codex_recovery import ExtractionAttempt
from autoscout.codex_recovery import ExtractionStatus
from autoscout.codex_recovery import ProductionCodexAdapter
from autoscout.extraction_round import ExtractionRoundCompletion
from autoscout.extraction_round import execute_bounded_extraction_round
from autoscout.russian_workflow import extraction_progress_message


def _round(items: list[dict], *, run, boundary, on_completion=None, ceiling: float = 30.0):
    clock = time.monotonic
    return execute_bounded_extraction_round(
        items,
        worker_count=1,
        per_attempt_ceiling=ceiling,
        portfolio_deadline=clock() + 600.0,
        clock=clock,
        run=run,
        boundary=boundary,
        on_completion=on_completion,
    )


class _UnavailableAgent:
    def extract(self, item, capture_path, artifact_path):
        return ExtractionAttempt(ExtractionStatus.UNAVAILABLE, False, diagnostic="test_agent_disabled")


def _valid_entries() -> list[dict]:
    return [
        {
            "title": "Book",
            "primary_authors": ["Author"],
            "source_url": "https://example.test/book",
            "evidence_url": "https://example.test/book",
            "evidence_note": "Listed book.",
            "candidate_category": "popular science",
            "signal_type": "award shortlist",
            "signal_strength": "shortlist",
        }
    ]


def _write_portfolio(
    output_dir, count: int, *, prevalidated: set[int] = frozenset(), missing_capture: set[int] = frozenset()
):
    """Write a worklist shaped like the real one.

    ``prevalidated`` sources already hold a valid artifact, and
    ``missing_capture`` sources have no capture file. Neither kind ever enters an
    extraction round, so neither ever produces a completion.
    """
    items = []
    for index in range(count):
        capture_file = f"capture-{index}.json"
        capture_path = output_dir / capture_file
        if index not in missing_capture:
            capture_path.write_text(
                json.dumps(
                    {
                        "source_url": f"https://example.test/{index}",
                        "html": '<a href="https://example.test/book">Book</a>',
                    }
                ),
                encoding="utf-8",
            )
        item = {
            "source_name": f"Source {index}",
            "source_url": f"https://example.test/{index}",
            "source_family": "award",
            "capture_file": capture_file,
            "expected_extraction_file": f"extraction-{index}.json",
            "operation_id": "test-operation",
            "capture_sha256": (
                hashlib.sha256(capture_path.read_bytes()).hexdigest() if index not in missing_capture else "0" * 64
            ),
        }
        items.append(item)
        if index in prevalidated:
            (output_dir / item["expected_extraction_file"]).write_text(
                json.dumps(
                    {
                        "source_name": item["source_name"],
                        "source_url": item["source_url"],
                        "operation_id": item["operation_id"],
                        "capture_sha256": item["capture_sha256"],
                        "entries": _valid_entries(),
                    }
                ),
                encoding="utf-8",
            )
    (output_dir / "codex-extraction-worklist.json").write_text(
        json.dumps({"run_date": "2026-08-05", "work_items": items}), encoding="utf-8"
    )


# ── The round seam ──────────────────────────────────────────────────────


def test_each_completion_is_observed() -> None:
    items = [{"source_name": f"Source {index}"} for index in range(3)]
    seen: list[str] = []

    _round(
        items,
        run=lambda context: {"diagnostics": []},
        boundary=lambda context, stage, threshold, supplier: {"diagnostics": []},
        on_completion=lambda completion: seen.append(str(completion.context.item["source_name"])),
    )

    assert sorted(seen) == ["Source 0", "Source 1", "Source 2"]


def test_round_is_unchanged_when_no_observer_is_supplied() -> None:
    """The calibration and replay paths must not change behavior."""
    completions = _round(
        [{"source_name": "Only"}],
        run=lambda context: {"diagnostics": [], "ok": True},
        boundary=lambda context, stage, threshold, supplier: {"diagnostics": []},
    )

    assert len(completions) == 1
    assert completions[0].result["ok"] is True


def test_a_raising_observer_never_breaks_the_round() -> None:
    def _explode(completion: ExtractionRoundCompletion) -> None:
        raise RuntimeError("progress reporting must never bound the round")

    completions = _round(
        [{"source_name": "First"}, {"source_name": "Second"}],
        run=lambda context: {"diagnostics": []},
        boundary=lambda context, stage, threshold, supplier: {"diagnostics": []},
        on_completion=_explode,
    )

    assert len(completions) == 2


def test_a_timed_out_completion_is_observed_too() -> None:
    """Silence on a timeout would look identical to a working round."""
    seen: list[int] = []

    def _run(context):
        time.sleep(0.3)
        return {"diagnostics": []}

    _round(
        [{"source_name": "Slow"}],
        run=_run,
        boundary=lambda context, stage, threshold, supplier: {
            "diagnostics": [{"source_name": "Slow", "status": "timeout"}]
        },
        on_completion=lambda completion: seen.append(completion.completion_sequence),
        ceiling=0.05,
    )

    assert seen == [1]


# ── The portfolio counting ──────────────────────────────────────────────


def test_progress_reaches_the_portfolio_total_when_every_source_extracts(tmp_path) -> None:
    _write_portfolio(tmp_path, 3)
    emitted: list[tuple[int, int, int, int]] = []

    ProductionCodexAdapter(
        agent=_UnavailableAgent(),
        on_progress=lambda *counts: emitted.append(counts),
    ).fulfill(tmp_path)

    assert [completed for completed, _, _, _ in emitted] == [1, 2, 3]
    assert emitted[-1][0] == emitted[-1][1] == 3


def test_progress_reaches_the_total_when_sources_never_enter_a_round(tmp_path) -> None:
    """A reused artifact and a missing capture never produce a completion.

    Counting only completions meant the line could never reach the total, so the
    stage fell silent for the rest of its runtime — the exact failure this
    reporting exists to remove.
    """
    _write_portfolio(tmp_path, 3, prevalidated={1}, missing_capture={2})
    emitted: list[tuple[int, int, int, int]] = []

    ProductionCodexAdapter(
        agent=_UnavailableAgent(),
        on_progress=lambda *counts: emitted.append(counts),
    ).fulfill(tmp_path)

    assert emitted, "the stage must report even when most sources never enter a round"
    # The two sources that cannot enter a round are reported as a baseline, so
    # the count starts honest and still finishes at the portfolio total.
    assert emitted[0][0] == 2
    assert emitted[-1][0] == emitted[-1][1] == 3


def test_progress_never_decreases_and_never_exceeds_the_total(tmp_path) -> None:
    _write_portfolio(tmp_path, 4, prevalidated={0})
    emitted: list[tuple[int, int, int, int]] = []

    ProductionCodexAdapter(
        agent=_UnavailableAgent(),
        max_attempts=2,
        on_progress=lambda *counts: emitted.append(counts),
    ).fulfill(tmp_path)

    completed = [count for count, _, _, _ in emitted]
    assert completed == sorted(completed), f"progress must never rewind: {completed}"
    assert max(completed) == 4
    assert all(total == 4 for _, total, _, _ in emitted)


def test_a_retry_round_is_announced_so_the_stage_stays_audible(tmp_path) -> None:
    """The count only moves on a first settled result.

    Without a retry notice the stage would fall silent for the whole retry round
    after the count had already filled.
    """

    class _RetryableFailureAgent:
        def extract(self, item, capture_path, artifact_path):
            return ExtractionAttempt(ExtractionStatus.COMMAND_FAILED, True, diagnostic="transient")

    _write_portfolio(tmp_path, 2)
    emitted: list[tuple[int, int, int, int]] = []

    ProductionCodexAdapter(
        agent=_RetryableFailureAgent(),
        max_attempts=2,
        on_progress=lambda *counts: emitted.append(counts),
        # Preserve this small-budget retry seam explicitly after the
        # production default per-attempt ceiling changed to 1800 seconds.
        per_attempt_timeout_seconds=180,
    ).fulfill(tmp_path)

    retrying = [count for *_, count in emitted]
    assert any(count > 0 for count in retrying), f"no retry round was announced: {emitted}"
    # The retry notice must not disturb the portfolio count.
    completed = [count for count, _, _, _ in emitted]
    assert completed == sorted(completed)
    assert max(completed) == 2


def test_fulfill_stays_silent_without_a_progress_sink(tmp_path) -> None:
    _write_portfolio(tmp_path, 1)

    result = ProductionCodexAdapter(agent=_UnavailableAgent()).fulfill(tmp_path)

    assert isinstance(result, dict)


def test_a_raising_sink_never_breaks_the_extraction_stage(tmp_path) -> None:
    """Not every emit goes through the round's observer guard.

    The baseline and the retry announce are emitted directly, so the swallow has
    to live with the emitter. The production sink prints Russian text to a pipe,
    so a BrokenPipeError or a UnicodeEncodeError must never end a 27-minute
    extraction stage because reporting failed.
    """

    class _RetryableFailureAgent:
        def extract(self, item, capture_path, artifact_path):
            return ExtractionAttempt(ExtractionStatus.COMMAND_FAILED, True, diagnostic="transient")

    def _explode(*counts: int) -> None:
        raise RuntimeError("reporting must never end the stage")

    # One source without a capture forces the baseline emit, and a retryable
    # failure forces the retry announce, so both direct call sites are covered.
    _write_portfolio(tmp_path, 3, missing_capture={2})

    result = ProductionCodexAdapter(
        agent=_RetryableFailureAgent(),
        max_attempts=2,
        on_progress=_explode,
    ).fulfill(tmp_path)

    assert isinstance(result, dict)
    # Swallowing silently would make a wiring bug look like a working stage
    # that reports nothing, so the first failure is recorded once.
    failures = [
        diagnostic
        for diagnostic in result.get("diagnostics", [])
        if diagnostic.get("status") == "progress_reporting_failed"
    ]
    assert len(failures) == 1, f"expected exactly one recorded sink failure, got {failures}"
    assert failures[0]["diagnostic"] == "RuntimeError"


def test_a_sink_with_the_wrong_arity_is_recorded_not_hidden(tmp_path) -> None:
    """A sink that cannot accept the counts it is given is a wiring bug.

    The CLI sink is ``lambda *counts: ...``, so it accepts any arity and a future
    count would raise inside the formatter instead. Either way the stage must
    still finish, and the failure must be visible rather than silent.
    """
    _write_portfolio(tmp_path, 2)

    def _too_few_parameters(completed: int) -> None:  # missing three counts
        raise AssertionError("unreachable: the call itself raises TypeError")

    result = ProductionCodexAdapter(agent=_UnavailableAgent(), on_progress=_too_few_parameters).fulfill(tmp_path)

    failures = [
        diagnostic
        for diagnostic in result.get("diagnostics", [])
        if diagnostic.get("status") == "progress_reporting_failed"
    ]
    assert len(failures) == 1
    assert failures[0]["diagnostic"] == "TypeError"


# ── The editor-facing wording ───────────────────────────────────────────


def test_progress_message_states_counts_timeouts_and_retries() -> None:
    assert extraction_progress_message(5, 29) == "Обработано 5 из 29 источников..."
    assert extraction_progress_message(12, 29, 4) == "Обработано 12 из 29 источников (не успели: 4)..."
    assert extraction_progress_message(29, 29, 4, 3) == (
        "Обработано 29 из 29 источников (не успели: 4) (повторная попытка: 3)..."
    )
    assert "не успели" not in extraction_progress_message(3, 29, 0)
    assert "повторная попытка" not in extraction_progress_message(3, 29, 0, 0)


def test_progress_message_contains_no_developer_diagnostics() -> None:
    """The clean editor-chat output boundary forbids paths and Git mechanics."""
    message = extraction_progress_message(7, 29, 2, 1)

    assert "/" not in message
    assert "git" not in message.lower()


def test_cli_formats_the_counts_at_the_same_seam_as_stage_messages(tmp_path) -> None:
    """Both progress seams carry data; the caller owns the wording."""
    from pathlib import Path

    from autoscout.weekly_workflow import weekly_adapters

    printed: list[str] = []
    adapters = weekly_adapters(
        Path("."),
        progress=lambda stage: None,
        extraction_progress=lambda *counts: printed.append(extraction_progress_message(*counts)),
    )
    assert adapters.codex._on_progress is not None
    adapters.codex._on_progress(12, 29, 4, 0)

    assert printed == ["Обработано 12 из 29 источников (не успели: 4)..."]
    assert weekly_adapters(Path(".")).codex._on_progress is None
