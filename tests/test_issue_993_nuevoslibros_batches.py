"""Issue #993: NuevosLibros weekly extraction covers the recent window, in batches.

The trimmed fixture is cut from the committed 2026-11-07 Política y sociedad
capture. Its reference date is 2026-11-07, so the window starts 2026-10-24.
These tests fake the Codex subprocess, so they spend no Codex request.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from datetime import date
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

import autoscout.codex_recovery as codex_recovery
from autoscout.codex_recovery import EMPTY_ANSWER_FOLLOWUP_PROMPT
from autoscout.codex_recovery import CodexCliExtractionAgent
from autoscout.codex_recovery import ExtractionStatus
from autoscout.codex_recovery import _merge_batch_answers
from autoscout.codex_recovery import build_codex_extraction_prompt
from autoscout.extraction_completeness import capture_listing_row_count
from autoscout.nuevoslibros_frontlist import FAMILY
from autoscout.nuevoslibros_frontlist import NO_RECENT_CARD_REASON
from autoscout.nuevoslibros_frontlist import BookCard
from autoscout.nuevoslibros_frontlist import capture_reference_date
from autoscout.nuevoslibros_frontlist import frontlist_cards
from autoscout.nuevoslibros_frontlist import recent_cards
from autoscout.observed_extraction import validate_extraction_artifact

FIXTURE = Path(__file__).parent / "fixtures" / "issue-993-nuevoslibros-window-capture.json"
CAPTURE = json.loads(FIXTURE.read_text(encoding="utf-8"))
URL = CAPTURE["source_url"]
REFERENCE = date(2026, 11, 7)
SOURCE_METADATA: dict[str, Any] = {
    "url": URL,
    "contract_family": FAMILY,
    "language": "es",
    "family": "media/curated list",
    "signal_family": "media/curated list",
    "expected_signal_strength": "major media list",
    "eligible_candidate_category": "adult nonfiction",
    "expected_page_heading": "Novedades de política y sociedad",
}
ALL_CARDS = frontlist_cards(CAPTURE["html"], URL)
RECENT_URLS = [card.url for card in ALL_CARDS if card.release_date >= "2026-10-24"]
OLDER_URLS = [card.url for card in ALL_CARDS if card.release_date < "2026-10-24"]


def _item(capture_path: Path, *, source_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "source_name": CAPTURE["source_name"],
        "source_url": URL,
        "operation_id": "a" * 64,
        "capture_sha256": hashlib.sha256(capture_path.read_bytes()).hexdigest(),
        "source_metadata": dict(SOURCE_METADATA if source_metadata is None else source_metadata),
    }


def _write_capture(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "capture.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _entry(card: BookCard) -> dict[str, Any]:
    return {
        "title": card.title,
        "primary_authors": list(card.authors),
        "publication_year": int(card.release_date[:4]),
        "source_url": URL,
        "evidence_url": card.url,
        "signal_type": "media/curated list",
        "signal_strength": "major media list",
        "candidate_category": "adult nonfiction",
        "evidence_note": "Dated frontlist entry.",
        "english_title": card.title,
        "search_translation": card.title,
        "author_transliteration_note": "",
    }


class BatchCodex:
    """Answers each Codex call in turn from the batch capture it was given."""

    def __init__(self, agent: CodexCliExtractionAgent, *, answers: dict[int, Any] | None = None) -> None:
        self._agent = agent
        self._answers = answers or {}
        self.captures: list[dict[str, Any]] = []
        self.commands: list[list[str]] = []
        self.timeouts: list[float] = []

    def __call__(self, command, isolated, response_path, bounded_timeout):
        number = len(self.commands) + 1
        self.commands.append(list(command))
        self.timeouts.append(bounded_timeout)
        capture = json.loads((isolated / "capture.json").read_text(encoding="utf-8"))
        self.captures.append(capture)
        telemetry = self._agent._telemetry
        telemetry.subprocess_started_at = 100.0 * number
        telemetry.subprocess_finished_at = 100.0 * number + 10.0
        telemetry.progress_event_counts = {"reasoning": 1}
        telemetry.codex_session_id = f"session-{number}"
        telemetry.rollout_path = f"rollout-{number}.jsonl"
        answer = self._answers.get(number)
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, tuple):
            return answer
        if answer is None:
            cards = frontlist_cards(capture["html"], URL)
            answer = {"entries": [_entry(card) for card in cards]}
        response_path.write_text(json.dumps(answer, ensure_ascii=False), encoding="utf-8")
        return (0, "")


def _run(tmp_path: Path, capture_path: Path, item: dict[str, Any], answers: dict[int, Any] | None = None):
    artifact = tmp_path / "artifact.json"
    agent = CodexCliExtractionAgent(timeout_seconds=600.0)
    fake = BatchCodex(agent, answers=answers)
    with mock.patch.object(CodexCliExtractionAgent, "_run_codex", side_effect=fake):
        attempt = agent.extract_with_timeout(item, capture_path, artifact, timeout_seconds=600.0)
    return attempt, artifact, fake


def _batch_size(monkeypatch: pytest.MonkeyPatch, size: int) -> None:
    monkeypatch.setattr(codex_recovery, "NUEVOSLIBROS_CODEX_BATCH_CARDS", size)


# 1. The recent-card window -------------------------------------------------


def test_window_keeps_recent_forthcoming_cards_and_drops_older_ones() -> None:
    kept = [card.url for card in recent_cards(CAPTURE["html"], URL, REFERENCE)]

    assert kept == RECENT_URLS
    assert len(kept) == 5
    assert any(card.release_date == "2027-01-27" for card in recent_cards(CAPTURE["html"], URL, REFERENCE))
    assert not set(kept) & set(OLDER_URLS)


def test_day_dates_compare_directly_against_the_window_start() -> None:
    html = CAPTURE["html"]
    on_start = html.replace('data-fecha="2026-10-22"', 'data-fecha="2026-10-24"', 1)
    before_start = html.replace('data-fecha="2026-10-22"', 'data-fecha="2026-10-23"', 1)

    assert OLDER_URLS[0] in [card.url for card in recent_cards(on_start, URL, REFERENCE)]
    assert OLDER_URLS[0] not in [card.url for card in recent_cards(before_start, URL, REFERENCE)]


def test_month_precision_dates_count_by_their_last_day() -> None:
    html = CAPTURE["html"]
    october = html.replace('data-fecha="2026-10-22"', 'data-fecha="2026-10"', 1)
    september = html.replace('data-fecha="2026-09-10"', 'data-fecha="2026-09"', 1)

    # A card dated 2026-10 is kept: its last day, Oct 31, is inside the window.
    assert OLDER_URLS[0] in [card.url for card in recent_cards(october, URL, REFERENCE)]
    # A card dated 2026-09 is dropped: its last day, Sep 30, is before Oct 24.
    assert OLDER_URLS[1] not in [card.url for card in recent_cards(september, URL, REFERENCE)]


def test_validation_grounding_still_sees_every_card() -> None:
    assert len(ALL_CARDS) > len(recent_cards(CAPTURE["html"], URL, REFERENCE))
    assert {card.url for card in ALL_CARDS} >= set(OLDER_URLS)


@pytest.mark.parametrize("reference", [None, "", "2026-13-01", "2026-11-7x", 20261107])
def test_missing_or_unreadable_reference_date_refuses(reference: Any) -> None:
    capture: dict[str, Any] = {"html": CAPTURE["html"]}
    if reference is not None:
        capture["reference_date"] = reference

    with pytest.raises(ValueError):
        capture_reference_date(capture)


def test_completeness_count_follows_the_window_and_refuses_without_a_reference_date() -> None:
    identity = {"source_url": URL, **SOURCE_METADATA}

    assert capture_listing_row_count(identity, CAPTURE) == len(RECENT_URLS)
    assert capture_listing_row_count(identity, {"html": CAPTURE["html"]}) is None


def test_merge_keeps_the_first_entry_of_each_evidence_url() -> None:
    first = {"entries": [{"evidence_url": "https://nuevoslibros.com/libro/a/", "title": "A"}], "empty_reason": "x"}
    second = {
        "entries": [
            {"evidence_url": "https://nuevoslibros.com/libro/a/", "title": "A again"},
            {"evidence_url": "https://nuevoslibros.com/libro/b/", "title": "B"},
        ]
    }

    item = {"source_name": "NL", "source_url": URL, "operation_id": "b" * 64, "capture_sha256": "c" * 64}

    merged = _merge_batch_answers([first, second], item)

    assert merged == {
        "source_name": "NL",
        "source_url": URL,
        "operation_id": "b" * 64,
        "capture_sha256": "c" * 64,
        "entries": [
            {"evidence_url": "https://nuevoslibros.com/libro/a/", "title": "A"},
            {"evidence_url": "https://nuevoslibros.com/libro/b/", "title": "B"},
        ],
    }


# 2. Batching in the Codex path ---------------------------------------------


def test_recent_cards_are_sent_in_batches_and_merged_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _batch_size(monkeypatch, 2)
    capture_path = _write_capture(tmp_path, CAPTURE)

    attempt, artifact, fake = _run(tmp_path, capture_path, _item(capture_path))

    batches = math.ceil(len(RECENT_URLS) / 2)
    assert attempt.status == ExtractionStatus.PRODUCED
    assert len(fake.commands) == batches == 3
    for capture in fake.captures:
        urls = [card.url for card in frontlist_cards(capture["html"], URL)]
        assert urls
        assert set(urls) <= set(RECENT_URLS)
        assert not set(urls) & set(OLDER_URLS)
        assert capture["reference_date"] == CAPTURE["reference_date"]
    assert [len(frontlist_cards(c["html"], URL)) for c in fake.captures] == [2, 2, 1]

    written = json.loads(artifact.read_text(encoding="utf-8"))
    assert [entry["evidence_url"] for entry in written["entries"]] == RECENT_URLS
    assert written["operation_id"] == "a" * 64
    observation = validate_extraction_artifact(artifact, _item(capture_path), capture_path)
    assert observation.valid, observation.diagnostic_class


def test_the_attempt_spans_every_batch_and_sums_the_census(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _batch_size(monkeypatch, 2)
    capture_path = _write_capture(tmp_path, CAPTURE)

    attempt, _artifact, fake = _run(tmp_path, capture_path, _item(capture_path))

    assert attempt.subprocess_started_at == 100.0
    assert attempt.subprocess_finished_at == 310.0
    assert attempt.subprocess_duration_seconds == 210.0
    assert attempt.diagnosis.progress_event_counts == {"reasoning": 3}
    assert attempt.diagnosis.codex_session_id == "session-3"
    assert attempt.diagnosis.rollout_path == "rollout-3.jsonl"
    assert attempt.prompt_characters == sum(
        len(build_codex_extraction_prompt(_item(capture_path))) for _ in fake.commands
    )
    assert all(0 < timeout <= 600.0 for timeout in fake.timeouts)


def test_each_batch_gets_its_own_isolated_capture_and_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _batch_size(monkeypatch, 2)
    capture_path = _write_capture(tmp_path, CAPTURE)

    _attempt, _artifact, fake = _run(tmp_path, capture_path, _item(capture_path))

    assert len({command[-2] for command in fake.commands}) == 3
    prompts = {command[-1] for command in fake.commands}
    assert len(prompts) == 1
    assert all("NuevosLibros frontlist contract:" in prompt for prompt in prompts)


# 3. Empty and failed batches -----------------------------------------------


def test_a_batch_with_cards_but_no_entries_fails_the_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _batch_size(monkeypatch, 2)
    capture_path = _write_capture(tmp_path, CAPTURE)
    empty = {"entries": [], "empty_reason": "The capture lacks sufficient metadata."}

    attempt, artifact, fake = _run(tmp_path, capture_path, _item(capture_path), answers={2: empty})

    assert attempt.status == ExtractionStatus.INVALID_OUTPUT
    assert attempt.diagnostic == "unexpected_empty_extraction"
    assert attempt.retryable is True
    assert not artifact.exists()
    assert len(fake.commands) == 2


def test_an_unreasoned_empty_batch_is_followed_up_then_fails_when_still_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _batch_size(monkeypatch, 2)
    capture_path = _write_capture(tmp_path, CAPTURE)
    empty = {"entries": []}

    attempt, artifact, fake = _run(tmp_path, capture_path, _item(capture_path), answers={1: empty, 2: empty})

    assert attempt.status == ExtractionStatus.INVALID_OUTPUT
    assert attempt.diagnostic == "unexpected_empty_extraction"
    assert not artifact.exists()
    assert len(fake.commands) == 2
    assert attempt.diagnosis.empty_answer_followup == codex_recovery.EMPTY_ANSWER_FOLLOWUP_UNCHANGED
    assert fake.commands[1][-1] == EMPTY_ANSWER_FOLLOWUP_PROMPT


@pytest.mark.parametrize("failing_batch", [1, 2])
def test_a_batch_command_failure_fails_the_attempt_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_batch: int
) -> None:
    _batch_size(monkeypatch, 2)
    capture_path = _write_capture(tmp_path, CAPTURE)

    attempt, artifact, fake = _run(
        tmp_path, capture_path, _item(capture_path), answers={failing_batch: (1, "unexpected failure")}
    )

    assert attempt.status == ExtractionStatus.COMMAND_FAILED
    assert attempt.exit_code == 1
    assert attempt.retryable is True
    assert not artifact.exists()
    assert len(fake.commands) == failing_batch


def test_a_batch_timeout_fails_the_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _batch_size(monkeypatch, 2)
    capture_path = _write_capture(tmp_path, CAPTURE)
    timeout = subprocess.TimeoutExpired(cmd="codex", timeout=600.0)

    attempt, artifact, _fake = _run(tmp_path, capture_path, _item(capture_path), answers={2: timeout})

    assert attempt.status == ExtractionStatus.TIMEOUT
    assert not artifact.exists()


def test_a_batch_with_invalid_json_fails_the_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _batch_size(monkeypatch, 2)
    capture_path = _write_capture(tmp_path, CAPTURE)

    attempt, artifact, _fake = _run(tmp_path, capture_path, _item(capture_path), answers={1: ["not", "an", "object"]})

    assert attempt.status == ExtractionStatus.INVALID_OUTPUT
    assert attempt.diagnostic == "non_object_json"
    assert not artifact.exists()


# 4. Captures with no recent card, and completeness --------------------------


def test_a_capture_without_recent_cards_makes_no_call_and_gives_an_empty_result_with_reason(tmp_path: Path) -> None:
    # The window of this reference date starts 2027-02-15: every card is older.
    capture_path = _write_capture(tmp_path, {**CAPTURE, "reference_date": "2027-03-01"})
    item = _item(capture_path)

    attempt, artifact, fake = _run(tmp_path, capture_path, item)

    assert attempt.status == ExtractionStatus.PRODUCED
    assert fake.commands == []
    written = json.loads(artifact.read_text(encoding="utf-8"))
    assert written["entries"] == []
    assert written["empty_reason"] == NO_RECENT_CARD_REASON
    assert written["capture_sha256"] == item["capture_sha256"]
    assert validate_extraction_artifact(artifact, item, capture_path).valid


def test_an_empty_artifact_for_a_capture_with_recent_cards_is_refused(tmp_path: Path) -> None:
    capture_path = _write_capture(tmp_path, CAPTURE)
    item = _item(capture_path)
    artifact = tmp_path / "empty.json"
    artifact.write_text(
        json.dumps(
            {
                "source_name": item["source_name"],
                "source_url": URL,
                "operation_id": item["operation_id"],
                "capture_sha256": item["capture_sha256"],
                "entries": [],
                "empty_reason": "No book listed.",
            }
        ),
        encoding="utf-8",
    )

    observation = validate_extraction_artifact(artifact, item, capture_path)

    assert not observation.valid
    assert observation.diagnostic_class == "unexpected_empty_extraction"


def test_a_capture_missing_its_reference_date_is_refused_before_any_call(tmp_path: Path) -> None:
    capture = {key: value for key, value in CAPTURE.items() if key != "reference_date"}
    capture_path = _write_capture(tmp_path, capture)

    attempt, artifact, fake = _run(tmp_path, capture_path, _item(capture_path))

    assert attempt.status == ExtractionStatus.UNAVAILABLE
    assert attempt.diagnostic == "frontlist_model_input_invalid_or_over_budget"
    assert fake.commands == []
    assert not artifact.exists()


# 5. Prompt -----------------------------------------------------------------


def test_the_nuevoslibros_prompt_carries_the_contract_paragraph() -> None:
    item = {"source_name": "NuevosLibros", "source_url": URL, "source_metadata": dict(SOURCE_METADATA)}

    prompt = build_codex_extraction_prompt(item)

    assert codex_recovery.NUEVOSLIBROS_FRONTLIST_PROMPT in prompt
    assert prompt.index("NuevosLibros frontlist contract:") < prompt.index("\nBefore answering, ")
    assert "A capture that holds cards is never empty." in prompt


def test_a_non_nuevoslibros_prompt_is_unchanged() -> None:
    item = {"source_name": "Other", "source_url": "https://example.test/list", "source_metadata": {"url": "x"}}

    prompt = build_codex_extraction_prompt(item)

    assert "NuevosLibros frontlist contract" not in prompt
    assert prompt.endswith(
        "The retained capture.json is the complete and only input for this task. "
        "Do not read a memory folder, a prior-run note, or any other workspace history."
    )


# 6. Platform API route with an empty recent window ----------------------------


def test_the_platform_api_route_returns_an_empty_result_without_a_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoscout.source_collectors.agent_backends import RealAgentExtractionBackend

    monkeypatch.setenv("AUTOSCOUT_TEST_KEY", "test-only")
    source = {
        **SOURCE_METADATA,
        "enable_real_backend": True,
        "credential_env": "AUTOSCOUT_TEST_KEY",
        "model_max_output_tokens": 8000,
    }
    # Every card predates the window of this reference date, so no batch exists.
    page_data = {**CAPTURE, "reference_date": "2027-03-01"}

    class FailingTransport:
        def extract(self, **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
            raise AssertionError("no model call may be made for a capture with no recent card")

    result, debug = RealAgentExtractionBackend(transport=FailingTransport()).extract(
        tmp_path / "config.json", source, page_data
    )

    assert result == {"mentions": [], "empty_reason": NO_RECENT_CARD_REASON}
    assert debug["model_request_count"] == 0


def test_a_capture_without_recent_cards_is_classified_as_a_named_failure_with_its_reason() -> None:
    from autoscout.source_contribution import zero_contribution_outcome

    # The Codex collector passes the extractor's empty reason as extractor_empty_reason.
    # Capture-size keys are omitted here; the classification only needs the reason.
    status, reason = zero_contribution_outcome(
        {**SOURCE_METADATA, "name": "NuevosLibros — Política y sociedad"},
        extracted_count=0,
        valid_mention_count=0,
        admissible_mention_count=0,
        debug_metadata={"extractor_empty_reason": NO_RECENT_CARD_REASON},
    )

    assert status == "failed"
    assert NO_RECENT_CARD_REASON in reason
