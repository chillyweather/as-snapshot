"""Retained evidence for a Codex no-progress timeout: the progress lease census and the attempt diagnostic."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from autoscout.codex_recovery import AttemptDiagnosis
from autoscout.codex_recovery import CodexCliExtractionAgent
from autoscout.codex_recovery import CodexProgressLease
from autoscout.codex_recovery import ExtractionAttempt
from autoscout.codex_recovery import ExtractionStatus
from autoscout.codex_recovery import _apply_local_result
from autoscout.codex_recovery import attempt_diagnosis_evidence
from autoscout.runtime_budget import CODEX_NO_PROGRESS_TIMEOUT_SECONDS

RECOGNIZED_LINE = b'{"type":"response_item","payload":{"type":"reasoning"},"timestamp":"2026-09-03T08:11:02.243Z"}\n'
IGNORED_LINE = b'{"type":"event_msg","payload":{"type":"item_completed"},"timestamp":"2026-09-03T08:11:02.239Z"}\n'


def test_an_ignored_event_is_counted_and_does_not_renew_the_lease() -> None:
    lease = CodexProgressLease()
    lease.begin(10.0)
    lease.feed(IGNORED_LINE, 20.0)

    # The census records the ignored event, so "the item was working and the
    # lease could not see it" is checkable. The lease itself is unmoved.
    assert lease.snapshot_unrecognized_counts() == {"event_msg/item_completed": 1}
    assert lease.snapshot_counts() == {}
    assert lease.idle_seconds(20.0) == 10.0

    lease.feed(RECOGNIZED_LINE, 30.0)
    assert lease.snapshot_counts() == {"reasoning": 1}
    assert lease.idle_seconds(30.0) == 0.0


def test_unparsed_and_non_object_lines_are_censused_without_progress() -> None:
    lease = CodexProgressLease()
    lease.begin(0.0)
    lease.feed(b"not json at all\n[1, 2, 3]\n\n", 5.0)

    assert lease.snapshot_unrecognized_counts() == {"unparsed": 1, "not_an_object": 1}
    # A blank line is separator noise, not an event.
    assert sum(lease.snapshot_unrecognized_counts().values()) == 2
    assert lease.idle_seconds(5.0) == 5.0


def test_the_census_key_count_is_bounded() -> None:
    lease = CodexProgressLease()
    lease.begin(0.0)
    for index in range(CodexProgressLease._MAX_CENSUS_KEYS + 20):
        lease.feed(json.dumps({"type": f"kind_{index}"}).encode("utf-8") + b"\n", 1.0)

    census = lease.snapshot_unrecognized_counts()
    assert len(census) == CodexProgressLease._MAX_CENSUS_KEYS + 1
    # Every kind past the limit is counted under one overflow key, so the
    # total survives even when the rare names do not.
    assert census["overflow"] == 20


def test_the_retained_tail_is_bounded_and_oldest_first() -> None:
    lease = CodexProgressLease()
    lease.begin(0.0)
    for index in range(CodexProgressLease._TAIL_RECORD_LIMIT + 5):
        line = json.dumps({"type": "event_msg", "payload": {"type": f"ignored_{index}"}}).encode("utf-8")
        lease.feed(line + b"\n", 1.0)

    tail = lease.snapshot_tail()
    assert len(tail) == CodexProgressLease._TAIL_RECORD_LIMIT
    first_retained = CodexProgressLease._TAIL_RECORD_LIMIT + 5 - CodexProgressLease._TAIL_RECORD_LIMIT
    assert tail[0]["kind"] == f"event_msg/ignored_{first_retained}"
    assert tail[-1]["kind"] == f"event_msg/ignored_{CodexProgressLease._TAIL_RECORD_LIMIT + 4}"


def test_the_tail_retains_model_authored_tool_input_and_never_page_content() -> None:
    lease = CodexProgressLease()
    lease.begin(0.0)
    call = {
        "type": "response_item",
        "timestamp": "2026-09-03T08:11:13.783Z",
        "payload": {
            "type": "custom_tool_call",
            "name": "exec",
            "input": "sed -n '1,240p' capture.json # token=sk-secretvalue0123456789",
        },
    }
    output = {
        "type": "response_item",
        "payload": {"type": "custom_tool_call_output", "output": "PAGE TEXT THAT MUST NOT BE RETAINED"},
    }
    lease.feed(json.dumps(call).encode("utf-8") + b"\n", 1.0)
    lease.feed(json.dumps(output).encode("utf-8") + b"\n", 2.0)

    tail = lease.snapshot_tail()
    assert tail[0]["tool_name"] == "exec"
    assert tail[0]["at"] == "2026-09-03T08:11:13.783Z"
    assert "capture.json" in tail[0]["tool_input_excerpt"]
    assert "sk-secretvalue0123456789" not in tail[0]["tool_input_excerpt"]
    # Tool output is page-derived. Only its size is retained.
    assert tail[1]["kind"] == "response_item/custom_tool_call_output"
    assert "PAGE TEXT THAT MUST NOT BE RETAINED" not in json.dumps(tail[1])
    assert tail[1]["bytes"] > 0


def test_the_tool_input_excerpt_is_bounded() -> None:
    lease = CodexProgressLease()
    lease.begin(0.0)
    call = {"type": "custom_tool_call", "name": "exec", "input": "x" * 5000}
    lease.feed(json.dumps(call).encode("utf-8") + b"\n", 1.0)

    excerpt = lease.snapshot_tail()[0]["tool_input_excerpt"]
    assert len(excerpt) == CodexProgressLease._TOOL_INPUT_EXCERPT_CHARS


def _stalled_agent(monkeypatch, rollout_lines: list[bytes], session_id: str, home: Path) -> CodexCliExtractionAgent:
    """Return an agent whose subprocess announces *session_id* and then stalls."""
    rollout_dir = home / "sessions" / "2026" / "09" / "03"
    rollout_dir.mkdir(parents=True)
    rollout = rollout_dir / f"rollout-2026-09-03T11-10-56-{session_id}.jsonl"
    rollout.write_bytes(b"".join(rollout_lines))

    class Process:
        pid = 449

        def __init__(self, *_args, **kwargs):
            # The real Codex CLI announces its session on stderr. Writing to
            # the same descriptor the agent handed us exercises the real
            # discovery path rather than a stub of it.
            os.write(kwargs["stderr"], f"session id: {session_id}\n".encode())

        def poll(self):
            return None

    class Clock:
        def __init__(self) -> None:
            self.value = 0.0

        def __call__(self) -> float:
            self.value += 1.0
            return self.value

    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(subprocess, "Popen", Process)
    monkeypatch.setattr("autoscout.codex_recovery._terminate_process_tree", lambda _process: None)
    return CodexCliExtractionAgent(
        timeout_seconds=CODEX_NO_PROGRESS_TIMEOUT_SECONDS + 10,
        clock=Clock(),
        sleeper=lambda _seconds: None,
    )


def test_a_no_progress_timeout_retains_its_session_rollout_census_and_tail(monkeypatch, tmp_path: Path) -> None:
    session_id = "01a06652-2c9f-7012-92a1-603050df2d65"
    agent = _stalled_agent(
        monkeypatch,
        [b'{"type":"event_msg","payload":{"type":"task_started"}}\n', IGNORED_LINE, RECOGNIZED_LINE],
        session_id,
        tmp_path / "codex-home",
    )
    isolated = tmp_path / "isolated"
    isolated.mkdir()

    with pytest.raises(subprocess.TimeoutExpired):
        agent._run_codex(["codex"], isolated, isolated / "response.json", CODEX_NO_PROGRESS_TIMEOUT_SECONDS + 10)

    assert agent._telemetry.boundary_reason == "no_progress_timeout"
    # The gap issue #449 named: an attempt could not be matched to the
    # rollout that recorded it. Both are now retained.
    assert agent._telemetry.codex_session_id == session_id
    assert agent._telemetry.rollout_path.endswith(f"rollout-2026-09-03T11-10-56-{session_id}.jsonl")
    assert agent._telemetry.progress_event_counts == {"task_started": 1, "reasoning": 1}
    assert agent._telemetry.unrecognized_event_counts == {"event_msg/item_completed": 1}
    assert [record["kind"] for record in agent._telemetry.progress_tail] == [
        "event_msg/task_started",
        "event_msg/item_completed",
        "response_item/reasoning",
    ]


def test_the_retained_evidence_reaches_the_extraction_attempt(monkeypatch, tmp_path: Path) -> None:
    session_id = "01a06652-2c9f-7012-92a1-603050df2d99"
    agent = _stalled_agent(
        monkeypatch,
        [b'{"type":"event_msg","payload":{"type":"task_started"}}\n'],
        session_id,
        tmp_path / "codex-home",
    )
    capture = tmp_path / "capture.json"
    capture.write_text(json.dumps({"html": "<html></html>"}), encoding="utf-8")

    attempt = agent.extract_with_timeout(
        {"source_name": "Source", "source_url": "https://example.test/source"},
        capture,
        tmp_path / "artifact.json",
        timeout_seconds=CODEX_NO_PROGRESS_TIMEOUT_SECONDS + 10,
    )

    assert attempt.status is ExtractionStatus.TIMEOUT
    assert attempt.diagnostic == "codex_no_progress_timeout"
    assert attempt.diagnosis.codex_session_id == session_id
    assert attempt.diagnosis.rollout_path.endswith(".jsonl")
    assert attempt.diagnosis.progress_event_counts == {"task_started": 1}
    assert attempt.diagnosis.progress_tail


def test_a_validated_attempt_states_its_counts_without_a_tail() -> None:
    attempt = ExtractionAttempt(
        ExtractionStatus.PRODUCED,
        True,
        diagnosis=AttemptDiagnosis(
            progress_event_counts={"reasoning": 4, "task_started": 1, "task_complete": 1},
            unrecognized_event_counts={"event_msg/item_completed": 9},
            progress_tail=({"kind": "response_item/reasoning", "recognized": True, "bytes": 12},),
            codex_session_id="session-1",
            rollout_path="/tmp/rollout.jsonl",
        ),
    )

    evidence = attempt_diagnosis_evidence(attempt, "validated")

    assert evidence["progress_event_counts"] == {"reasoning": 4, "task_started": 1, "task_complete": 1}
    assert evidence["codex_session_id"] == "session-1"
    # A healthy attempt has nothing to diagnose, so its tail is not carried.
    assert "progress_tail" not in evidence


def test_a_failed_attempt_carries_its_tail() -> None:
    attempt = ExtractionAttempt(
        ExtractionStatus.TIMEOUT,
        False,
        diagnostic="codex_no_progress_timeout",
        diagnosis=AttemptDiagnosis(
            progress_event_counts={"task_started": 1},
            progress_tail=({"kind": "event_msg/task_started", "recognized": True, "bytes": 40},),
        ),
    )

    evidence = attempt_diagnosis_evidence(attempt, "no_progress_timeout")

    assert evidence["progress_tail"] == [{"kind": "event_msg/task_started", "recognized": True, "bytes": 40}]


def test_a_missing_attempt_still_produces_evidence_keys() -> None:
    evidence = attempt_diagnosis_evidence(None, "worker_exception")

    assert evidence == {
        "progress_event_counts": {},
        "no_progress_quiet_seconds": None,
        "unrecognized_event_counts": {},
        "codex_session_id": "",
        "rollout_path": "",
    }


def test_the_written_attempt_diagnostic_carries_the_evidence(tmp_path: Path) -> None:
    """The evidence must reach the diagnostic the run artifacts keep.

    This is the record that issue #449 had to read and could not.
    """
    attempt = ExtractionAttempt(
        ExtractionStatus.TIMEOUT,
        False,
        diagnostic="codex_no_progress_timeout",
        diagnosis=AttemptDiagnosis(
            progress_event_counts={"task_started": 1, "reasoning": 3},
            no_progress_quiet_seconds=180.0,
            unrecognized_event_counts={"event_msg/item_completed": 7},
            progress_tail=({"kind": "response_item/reasoning", "recognized": True, "bytes": 4088},),
            codex_session_id="01a06652-2c9f-7012-92a1-603050df2d65",
            rollout_path="/sessions/rollout.jsonl",
        ),
    )
    state = {"item": {"source_name": "Source"}, "artifact_path": tmp_path / "artifact.json"}
    outcome = _apply_local_result(
        {
            "started": True,
            "extraction": attempt,
            "elapsed": 210.778,
            "staged_valid": False,
            "diagnostics": [],
            "staging_path": None,
            "progress_event_counts": attempt.diagnosis.progress_event_counts,
            "no_progress_quiet_seconds": 180.0,
        },
        state,
        1,
    )

    written = outcome["diagnostics"][-1]
    assert written["status"] == "no_progress_timeout"
    assert written["duration_seconds"] == 210.778
    assert written["progress_event_counts"] == {"task_started": 1, "reasoning": 3}
    assert written["no_progress_quiet_seconds"] == 180.0
    assert written["unrecognized_event_counts"] == {"event_msg/item_completed": 7}
    assert written["codex_session_id"] == "01a06652-2c9f-7012-92a1-603050df2d65"
    assert written["rollout_path"] == "/sessions/rollout.jsonl"
    assert written["progress_tail"] == [{"kind": "response_item/reasoning", "recognized": True, "bytes": 4088}]
    # The diagnostic must survive a JSON round trip, because that is how the
    # run log carries it.
    assert json.loads(json.dumps(written)) == written
