"""Issue #751: finish an empty answer the extractor gave no reason for.

Replaying the frozen benchmark showed the extractor listing a capture's rows
with its own tool calls and then answering ``entries: []`` with no
``empty_reason``.  Asked in the same session, it returned the rows.  These
tests fake the Codex subprocess, so they spend no Codex request.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from autoscout import model_qualification as mq
from autoscout.codex_recovery import EMPTY_ANSWER_FOLLOWUP_EXPLAINED
from autoscout.codex_recovery import EMPTY_ANSWER_FOLLOWUP_MIN_SECONDS
from autoscout.codex_recovery import EMPTY_ANSWER_FOLLOWUP_NOT_RUN
from autoscout.codex_recovery import EMPTY_ANSWER_FOLLOWUP_PROMPT
from autoscout.codex_recovery import EMPTY_ANSWER_FOLLOWUP_RECOVERED
from autoscout.codex_recovery import EMPTY_ANSWER_FOLLOWUP_UNCHANGED
from autoscout.codex_recovery import AttemptDiagnosis
from autoscout.codex_recovery import CodexCliExtractionAgent
from autoscout.codex_recovery import ExtractionStatus
from autoscout.codex_recovery import is_unreasoned_empty_answer
from autoscout.codex_recovery import subprocess as recovery_subprocess

SESSION_ID = "01a10716-3e83-73f2-a07a-aa339f8a38bf"
EMPTY = {"source_name": "Source", "entries": []}
ROWS = {"source_name": "Source", "entries": [{"title": "Sobrevivir en el Medievo"}]}


class FakeCodex:
    """Answers each Codex invocation in turn and records the commands."""

    def __init__(self, agent: CodexCliExtractionAgent, answers: list[Any], *, session_id: str = SESSION_ID) -> None:
        self._agent = agent
        self._answers = list(answers)
        self._session_id = session_id
        self._counts: list[dict[str, int]] = [{"reasoning": 3}, {"reasoning": 5, "task_complete": 2}]
        self.commands: list[list[str]] = []
        self.timeouts: list[float] = []

    def __call__(self, command, isolated, response_path, bounded_timeout):
        del isolated
        self.commands.append(list(command))
        self.timeouts.append(bounded_timeout)
        telemetry = self._agent._telemetry
        # The real runner resets the attempt telemetry on every invocation.
        telemetry.subprocess_started_at = 100.0 * len(self.commands)
        telemetry.subprocess_finished_at = 100.0 * len(self.commands) + 10.0
        # A resumed session's rollout holds the first invocation's events too,
        # so the follow-up's census is cumulative.
        telemetry.progress_event_counts = dict(self._counts.pop(0)) if self._counts else {"reasoning": 1}
        telemetry.codex_session_id = self._session_id if len(self.commands) == 1 else ""
        telemetry.rollout_path = "rollout.jsonl" if len(self.commands) == 1 else ""
        answer = self._answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, tuple):
            return answer
        response_path.write_text(answer if isinstance(answer, str) else json.dumps(answer), encoding="utf-8")
        return (0, "")


def _extract(tmp_path: Path, answers: list[Any], *, timeout: float = 600.0, session_id: str = SESSION_ID):
    capture = tmp_path / "capture.json"
    artifact = tmp_path / "artifact.json"
    capture.write_text("{}", encoding="utf-8")
    agent = CodexCliExtractionAgent(timeout_seconds=600.0)
    fake = FakeCodex(agent, answers, session_id=session_id)
    with mock.patch.object(CodexCliExtractionAgent, "_run_codex", side_effect=fake):
        attempt = agent.extract_with_timeout({}, capture, artifact, timeout_seconds=timeout)
    return attempt, json.loads(artifact.read_text(encoding="utf-8")), fake


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"entries": []}, True),
        ({"mentions": []}, True),
        ({"entries": [], "empty_reason": "   "}, True),
        ({"entries": [], "empty_reason": "The capture holds no nonfiction module."}, False),
        ({"entries": [{"title": "A"}]}, False),
        ({"source_name": "Source"}, False),
        ([], False),
    ],
)
def test_only_an_empty_answer_without_a_reason_is_unreasoned(payload: Any, expected: bool) -> None:
    assert is_unreasoned_empty_answer(payload) is expected


def test_the_same_session_is_asked_once_and_its_rows_become_the_artifact(tmp_path: Path) -> None:
    attempt, artifact, fake = _extract(tmp_path, [EMPTY, ROWS])

    assert attempt.status == ExtractionStatus.PRODUCED
    assert artifact == ROWS
    assert attempt.diagnosis.empty_answer_followup == EMPTY_ANSWER_FOLLOWUP_RECOVERED
    assert attempt.output_bytes == len(json.dumps(ROWS))
    assert len(fake.commands) == 2
    followup = fake.commands[1]
    assert followup[:4] == ["codex", "exec", "resume", SESSION_ID]
    assert followup[-1] == EMPTY_ANSWER_FOLLOWUP_PROMPT
    # The follow-up keeps the first invocation's isolation, features and pin.
    first = fake.commands[0]
    assert followup[followup.index("-c") + 1] == 'sandbox_mode="read-only"'
    assert "--skip-git-repo-check" in followup
    assert followup[followup.index("-m") + 1] == first[first.index("-m") + 1]
    assert [flag for flag in first if flag == "--disable"] == [flag for flag in followup if flag == "--disable"]


def test_the_attempt_spans_both_invocations_and_stays_bound_to_its_session(tmp_path: Path) -> None:
    attempt, _artifact, fake = _extract(tmp_path, [EMPTY, ROWS], timeout=300.0)

    assert attempt.subprocess_started_at == 100.0
    assert attempt.subprocess_finished_at == 210.0
    assert attempt.subprocess_duration_seconds == 110.0
    assert attempt.diagnosis.codex_session_id == SESSION_ID
    assert attempt.diagnosis.rollout_path == "rollout.jsonl"
    # The cumulative census is not added to the first invocation's.
    assert attempt.diagnosis.progress_event_counts == {"reasoning": 5, "task_complete": 2}
    # The follow-up only gets what the first invocation left of the bound.
    assert fake.timeouts == [300.0, 290.0]


def test_a_stated_reason_is_kept_as_the_answer(tmp_path: Path) -> None:
    explained = {"source_name": "Source", "entries": [], "empty_reason": "The module lists only fiction."}

    attempt, artifact, _fake = _extract(tmp_path, [EMPTY, explained])

    assert artifact == explained
    assert attempt.diagnosis.empty_answer_followup == EMPTY_ANSWER_FOLLOWUP_EXPLAINED


def test_an_answer_with_rows_or_a_reason_is_never_followed_up(tmp_path: Path) -> None:
    explained = {"entries": [], "empty_reason": "The capture holds a consent wall."}
    for answer in (ROWS, explained):
        attempt, artifact, fake = _extract(tmp_path, [answer])
        assert artifact == answer
        assert len(fake.commands) == 1
        assert attempt.diagnosis.empty_answer_followup == ""


@pytest.mark.parametrize(
    "second",
    [
        EMPTY,
        {},
        {"source_name": "Source", "empty_reason": "No rows."},
        {"entries": "none", "empty_reason": "No rows."},
        {"entries": None},
        "not json",
        ["not", "an", "object"],
        (1, "resume failed"),
        recovery_subprocess.TimeoutExpired(["codex"], 1.0),
        OSError("codex vanished"),
    ],
)
def test_a_follow_up_that_does_not_improve_the_answer_leaves_the_attempt_as_it_was(tmp_path: Path, second: Any) -> None:
    attempt, artifact, fake = _extract(tmp_path, [EMPTY, second])

    assert attempt.status == ExtractionStatus.PRODUCED
    assert attempt.retryable is True
    assert artifact == EMPTY
    assert attempt.output_bytes == len(json.dumps(EMPTY))
    assert attempt.diagnosis.empty_answer_followup == EMPTY_ANSWER_FOLLOWUP_UNCHANGED
    assert len(fake.commands) == 2


def test_no_follow_up_runs_without_a_session_or_without_time_left(tmp_path: Path) -> None:
    attempt, artifact, fake = _extract(tmp_path, [EMPTY], session_id="")
    assert artifact == EMPTY
    assert len(fake.commands) == 1
    assert attempt.diagnosis.empty_answer_followup == EMPTY_ANSWER_FOLLOWUP_NOT_RUN

    # The first invocation took 10 seconds of the bound.
    attempt, artifact, fake = _extract(tmp_path, [EMPTY], timeout=EMPTY_ANSWER_FOLLOWUP_MIN_SECONDS + 9.0)
    assert artifact == EMPTY
    assert len(fake.commands) == 1
    assert attempt.diagnosis.empty_answer_followup == EMPTY_ANSWER_FOLLOWUP_NOT_RUN


def test_the_follow_up_outcome_is_stated_in_attempt_evidence_only_when_one_was_needed() -> None:
    assert "empty_answer_followup" not in AttemptDiagnosis().evidence(retain_tail=True)
    evidence = AttemptDiagnosis(empty_answer_followup=EMPTY_ANSWER_FOLLOWUP_RECOVERED).evidence(retain_tail=False)
    assert evidence["empty_answer_followup"] == EMPTY_ANSWER_FOLLOWUP_RECOVERED


# -- Benchmark scoring -------------------------------------------------------


def test_a_reference_row_without_an_author_is_found_by_its_title() -> None:
    reference = {"valiant women|", "end times fascism|naomi klein"}

    assert mq.score_against_reference({"valiant women|lena andrews", "end times fascism|naomi klein"}, reference) == (
        2,
        0,
    )
    assert mq.score_against_reference({"valiant women|", "end times fascism|naomi klein"}, reference) == (2, 0)


def test_a_reference_row_with_an_author_still_needs_that_author() -> None:
    reference = {"end times fascism|naomi klein"}

    assert mq.score_against_reference({"end times fascism|astra taylor"}, reference) == (0, 1)
    assert mq.score_against_reference({"end times fascism|"}, reference) == (0, 1)


def test_an_author_is_the_same_author_in_either_name_order() -> None:
    reference = {"a matter of hope|bildner jim", "paseos con robert walser|seelig carl"}

    assert mq.score_against_reference(
        {"a matter of hope|jim bildner", "paseos con robert walser|carl seelig"}, reference
    ) == (2, 0)
    assert mq.score_against_reference({"a matter of hope|jim collins"}, reference) == (0, 1)


def test_one_answer_row_finds_at_most_one_reference_row() -> None:
    reference = {"the castle|"}

    assert mq.score_against_reference({"the castle|jon ronson", "the castle|franz kafka"}, reference) == (1, 1)
    assert mq.score_against_reference(set(), reference) == (0, 0)


def test_the_bookpage_reference_is_fully_found_by_an_answer_that_names_every_author() -> None:
    case = next(case for case in mq.load_benchmark() if case.source_name == "BookPage Nonfiction")
    reference = set(case.reference_keys())
    blank = {key for key in reference if key.endswith("|")}
    assert blank, "the frozen BookPage reference no longer holds a row without an author"

    answer = {key + "an author the page did not show" if key in blank else key for key in reference}

    assert mq.score_against_reference(answer, reference) == (len(reference), 0)


def test_a_stored_baseline_scored_under_another_recall_rule_is_rerun(tmp_path: Path) -> None:
    names = {"BookPage Nonfiction"}
    pin = mq.load_extraction_model_pin()
    stored = {
        "candidate": {**pin.as_profile(), "sources": {"BookPage Nonfiction": {"runs": 3, "mean_recall": 0.6061}}},
        "benchmark_digest": "digest",
    }
    report_path = tmp_path / "data" / "report.json"
    report_path.parent.mkdir()
    pin_path = tmp_path / "configs" / "codex-model-pin.json"
    pin_path.parent.mkdir()
    pin_document = {"qualification_report": "data/report.json"}

    report_path.write_text(json.dumps(stored), encoding="utf-8")
    summary, reason = mq._stored_baseline(pin, pin_document, pin_path, "digest", names, 3)
    assert summary is None
    assert reason == "the pinned qualification report scored recall under another rule"

    stored["recall_scoring_version"] = mq.RECALL_SCORING_VERSION
    report_path.write_text(json.dumps(stored), encoding="utf-8")
    summary, reason = mq._stored_baseline(pin, pin_document, pin_path, "digest", names, 3)
    assert summary == {"BookPage Nonfiction": {"runs": 3, "mean_recall": 0.6061}}
    assert reason == "data/report.json"


def test_the_first_census_stands_when_the_follow_up_never_read_the_rollout(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    capture.write_text("{}", encoding="utf-8")
    agent = CodexCliExtractionAgent(timeout_seconds=600.0)
    fake = FakeCodex(agent, [EMPTY, recovery_subprocess.TimeoutExpired(["codex"], 1.0)])
    fake._counts = [{"reasoning": 3}, {}]
    with mock.patch.object(CodexCliExtractionAgent, "_run_codex", side_effect=fake):
        attempt = agent.extract_with_timeout({}, capture, tmp_path / "artifact.json", timeout_seconds=600.0)

    assert attempt.diagnosis.progress_event_counts == {"reasoning": 3}
