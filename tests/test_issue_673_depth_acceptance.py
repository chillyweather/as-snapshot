"""Offline coverage for issue #673: depth acceptance and preservation.

New grounded/single-source synthesis and adjudication replacements must
clear the shared depth floor. Below-floor synthesis reports
``attempted_no_description`` with ``insufficient_description_depth`` and
never becomes a successful outcome. An unsuccessful attempt never
destroys retained usable text: diagnostics travel in the attempt log
while the content stays. Editor-corrected records stay protected, and
the legacy codex-generated path is untouched by the adjudication
backstop.
"""

from __future__ import annotations

from typing import Any

from state_store_support import read_state
from state_store_support import seed_state

from autoscout.content_description import DESCRIPTION_ADJUDICATED_PROVENANCE
from autoscout.content_description import DESCRIPTION_FALLBACK
from autoscout.content_description import DESCRIPTION_INSUFFICIENT_DEPTH_REASON
from autoscout.content_description import apply_description_artifacts
from autoscout.description_depth import DEPTH_MIN_CHARS
from autoscout.description_depth import DEPTH_MIN_SENTENCES
from autoscout.grounded_description import GROUNDED_SYNTHESIS_PROMPT
from autoscout.grounded_description import OUTCOME_ATTEMPTED_NO_DESCRIPTION
from autoscout.grounded_description import OUTCOME_GROUNDED
from autoscout.grounded_description import OUTCOME_SINGLE_SOURCE
from autoscout.grounded_description import REASON_CODES
from autoscout.grounded_description import REASON_FEWER_THAN_TWO
from autoscout.grounded_description import REASON_INSUFFICIENT_DEPTH
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import StageArtifact
from autoscout.grounded_description import StageOutcome
from autoscout.grounded_description import SynthesisResult
from autoscout.grounded_description import reason_label
from autoscout.grounded_description import validate_stage_artifact
from autoscout.json_io import read_json
from autoscout.json_io import write_json

OPERATION_DATE = "2026-09-14"
THIN_TEXT = "Книга о памяти и общественной жизни."
RICH_TEXT = (
    "Книга о памяти, обществе и общественной жизни маленького города. "
    "Автор соединяет репортаж с историческим контекстом и показывает, "
    "как прошлое формирует настоящее. "
    "Редакционный крючок — тихий повседневный ракурс вместо громких обобщений."
)
SUBJECT = "memory migration and public life"


def _candidate() -> dict[str, Any]:
    return {
        "candidate_id": "candidate-001",
        "identity_key": "test work|test author",
        "title": "Test Work",
        "primary_authors": ["Test Author"],
        "first_seen_date": OPERATION_DATE,
        "priority_label": "A",
    }


def _discovery(url: str, owner: str) -> dict[str, Any]:
    return {
        "url": url,
        "source_kind": "publisher",
        "source_family": "publisher_copy",
        "owner_id": owner,
        "source_language": "en",
        "classification": "classified",
    }


class _Discovery:
    def discover(self, _candidate: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            _discovery("https://one.example.test/work", "owner-one"),
            _discovery("https://two.example.test/work", "owner-two"),
        ]


class _Fetch:
    def fetch(self, result: Any, **kwargs: Any) -> dict[str, Any]:
        url = result.url if hasattr(result, "url") else str(result.get("url", ""))
        owner = result.owner_id if hasattr(result, "owner_id") else str(result.get("owner_id", ""))
        return {
            "url": url,
            "source_kind": "publisher",
            "source_family": "publisher_copy",
            "owner_id": owner,
            "source_language": "en",
            "title": "Test Work",
            "primary_authors": ["Test Author"],
            "subject_text": SUBJECT,
            "subject_key": SUBJECT,
            "checked_date": kwargs.get("checked_date", OPERATION_DATE),
            "classification": "classified",
        }


class _Synthesizer:
    def __init__(self, text: str) -> None:
        self.text = text

    def synthesize(self, _candidate: Any, sources: Any) -> SynthesisResult:
        return SynthesisResult(self.text)


def _run_stage(text: str) -> StageArtifact:
    return GroundedDescriptionStage(
        discovery=_Discovery(),  # type: ignore[arg-type]
        fetch=_Fetch(),  # type: ignore[arg-type]
        synthesizer=_Synthesizer(text),  # type: ignore[arg-type]
    ).run([_candidate()], operation_id="issue-673", operation_date=OPERATION_DATE, force_candidates=True)


# ── Stage acceptance ──────────────────────────────────────────────────


def test_thin_synthesis_is_attempted_no_description_with_depth_reason() -> None:
    artifact = _run_stage(THIN_TEXT)

    assert len(artifact.outcomes) == 1
    outcome = artifact.outcomes[0]
    assert outcome.outcome == OUTCOME_ATTEMPTED_NO_DESCRIPTION
    assert outcome.reason_code == REASON_INSUFFICIENT_DEPTH
    assert not outcome.content_description
    # Product rule changed by #684: a post-gate refusal retains the
    # verified accepted sources so a later repair pass can start from them
    # instead of re-discovering. Pre-gate refusals still carry none.
    assert [source.canonical_url for source in outcome.research_sources] == [
        "https://one.example.test/work",
        "https://two.example.test/work",
    ]
    assert artifact.counts["attempted_no_description"] == 1
    assert artifact.reason_counts == {REASON_INSUFFICIENT_DEPTH: 1}
    assert validate_stage_artifact(artifact)
    assert validate_stage_artifact(artifact.to_dict())


def test_rich_synthesis_still_grounds() -> None:
    artifact = _run_stage(RICH_TEXT)

    outcome = artifact.outcomes[0]
    assert outcome.outcome == OUTCOME_GROUNDED
    assert outcome.content_description == RICH_TEXT
    assert validate_stage_artifact(artifact.to_dict())


def test_depth_reason_is_registered_and_labeled() -> None:
    assert REASON_INSUFFICIENT_DEPTH == "insufficient_description_depth"
    assert REASON_INSUFFICIENT_DEPTH in REASON_CODES
    assert DESCRIPTION_INSUFFICIENT_DEPTH_REASON == REASON_INSUFFICIENT_DEPTH
    label = reason_label(REASON_INSUFFICIENT_DEPTH)
    assert label == "Описание короче порога глубины"
    assert label != reason_label("no_such_reason")


def test_synthesis_prompt_requires_depth_and_honest_omission() -> None:
    assert "2-4" in GROUNDED_SYNTHESIS_PROMPT
    assert "omit content_description" in GROUNDED_SYNTHESIS_PROMPT
    assert "accepted_bounded_evidence" in GROUNDED_SYNTHESIS_PROMPT
    assert "must not search" in GROUNDED_SYNTHESIS_PROMPT
    assert "Do not pad" in GROUNDED_SYNTHESIS_PROMPT
    assert "do not invent" in GROUNDED_SYNTHESIS_PROMPT


# ── Adjudication plan and apply backstop ──────────────────────────────


def _verification() -> dict[str, Any]:
    return {
        "resolved": True,
        "owner_count": 2,
        "counting_urls": ["https://one.example.test/work", "https://two.example.test/work"],
        "counting_digests": ["digest-one", "digest-two"],
    }


def _response(text: str) -> dict[str, Any]:
    return {
        "identity_key": "test work|test author",
        "resolved": True,
        "unresolved_reason": "",
        "confidence": "high",
        "content_description": text,
        "citations": [],
    }


def test_adjudication_plan_rejects_below_floor_text() -> None:
    from autoscout.description_adjudication import DECISION_FALLBACK
    from autoscout.description_adjudication import plan_adjudication_application

    plan = plan_adjudication_application(
        _response(THIN_TEXT), _verification(), identity_matched=True, candidate_id="candidate-001"
    )

    assert plan["decision"] == DECISION_FALLBACK
    assert plan["reason"] == REASON_INSUFFICIENT_DEPTH
    assert plan["artifact"]["rejection_reason"] == REASON_INSUFFICIENT_DEPTH


def test_adjudication_plan_accepts_depth_qualified_text() -> None:
    from autoscout.description_adjudication import DECISION_APPLY_TWO_SOURCE
    from autoscout.description_adjudication import plan_adjudication_application

    plan = plan_adjudication_application(
        _response(RICH_TEXT), _verification(), identity_matched=True, candidate_id="candidate-001"
    )

    assert plan["decision"] == DECISION_APPLY_TWO_SOURCE
    assert plan["artifact"]["content_description"] == RICH_TEXT


def _fallback_record() -> dict[str, Any]:
    return {
        "candidate_id": "candidate-001",
        "identity_key": "test work|test author",
        "title": "Test Work",
        "primary_authors": ["Test Author"],
        "content_description": DESCRIPTION_FALLBACK,
    }


def test_apply_gate_backstop_rejects_below_floor_adjudicated_text() -> None:
    # Even when the planning step is bypassed and the artifact is handed to
    # the shared gate directly, below-floor adjudicated text must not persist.
    updated, results = apply_description_artifacts(
        [_fallback_record()],
        [{"identity_key": "test work|test author", "content_description": THIN_TEXT}],
        provenance=DESCRIPTION_ADJUDICATED_PROVENANCE,
        effective_date=OPERATION_DATE,
    )

    assert results["applied"] == 0
    assert results["fallback"] == 1
    assert updated[0]["content_description"] == DESCRIPTION_FALLBACK
    assert updated[0]["content_description_rejection_reason"] == REASON_INSUFFICIENT_DEPTH


def test_apply_gate_accepts_depth_qualified_adjudicated_text() -> None:
    updated, results = apply_description_artifacts(
        [_fallback_record()],
        [{"identity_key": "test work|test author", "content_description": RICH_TEXT}],
        provenance=DESCRIPTION_ADJUDICATED_PROVENANCE,
        effective_date=OPERATION_DATE,
    )

    assert results["applied"] == 1
    assert updated[0]["content_description"] == RICH_TEXT
    assert "content_description_rejection_reason" not in updated[0]


def test_apply_gate_leaves_legacy_codex_path_untouched() -> None:
    # The backstop is scoped to adjudicated replacements (issue #673).
    # Legacy repair eligibility is issue #674's scope, not this gate's.
    updated, results = apply_description_artifacts(
        [_fallback_record()],
        [{"identity_key": "test work|test author", "content_description": THIN_TEXT}],
        provenance="codex-generated",
        effective_date=OPERATION_DATE,
    )

    assert results["applied"] == 1
    assert updated[0]["content_description"] == THIN_TEXT


# ── Preservation: failed attempts keep usable text ────────────────────


def _usable_codex_record() -> dict[str, Any]:
    return {
        "candidate_id": "candidate-001",
        "identity_key": "test work|test author",
        "title": "Test Work",
        "primary_authors": ["Test Author"],
        "content_description": RICH_TEXT,
        "description_provenance": {
            "identity_key": "test work|test author",
            "content_description": RICH_TEXT,
            "provenance": "codex-generated",
            "applied_date": "2026-09-01",
            "evidence_basis": ["https://one.example.test/work"],
        },
    }


class _SeededStage:
    stage_budget_seconds = 900.0
    candidate_envelope_seconds = 90.0
    discovery_budget_seconds = 10.0
    request_inactivity_timeout_seconds = 10.0
    max_workers = 1
    max_fetches = 3

    def __init__(self, outcomes: list[StageOutcome]) -> None:
        self._outcomes = list(outcomes)

    def run(self, candidates: Any, **kwargs: Any) -> StageArtifact:
        wanted = {str(item.get("candidate_id", "")) for item in candidates if isinstance(item, dict)}
        return StageArtifact(
            operation_id=str(kwargs.get("operation_id", "op")),
            operation_date=str(kwargs.get("operation_date", OPERATION_DATE)),
            outcomes=tuple(outcome for outcome in self._outcomes if outcome.candidate_id in wanted),
            queue=(),
        )


def _failed_outcome() -> StageOutcome:
    return StageOutcome(
        candidate_id="candidate-001",
        outcome=OUTCOME_ATTEMPTED_NO_DESCRIPTION,
        reason_code=REASON_FEWER_THAN_TWO,
        identity_key="test work|test author",
    )


def test_weekly_failed_attempt_preserves_usable_text(tmp_path: Any) -> None:
    from autoscout.grounded_description import GroundedDescriptionStageService

    state_dir = tmp_path / "state"
    output_dir = tmp_path / "run"
    state_dir.mkdir()
    output_dir.mkdir()
    seed_state(state_dir, candidate_history=[_usable_codex_record()])
    seed_state(state_dir, candidate_visibility=[])
    write_json(output_dir / "scouting-candidates.json", [_candidate()])
    service = GroundedDescriptionStageService(_SeededStage([_failed_outcome()]))  # type: ignore[arg-type]

    result = service.enrich(output_dir, state_dir, OPERATION_DATE, operation_id="issue-673")

    assert result["status"] == "ok"
    assert result["attempted_no_description"] == 1
    history = read_state(state_dir, "candidate_history")
    assert history[0]["content_description"] == RICH_TEXT
    assert history[0]["description_provenance"]["provenance"] == "codex-generated"
    assert "content_description_rejection_reason" not in history[0]
    # Diagnostics are separate from content: the attempt is logged.
    attempts = history[0]["grounded_description_attempts"]
    assert attempts[-1]["outcome"] == OUTCOME_ATTEMPTED_NO_DESCRIPTION
    assert attempts[-1]["reason_code"] == REASON_FEWER_THAN_TWO
    current = read_json(output_dir / "scouting-candidates.json")
    assert current[0]["content_description"] == RICH_TEXT


def test_weekly_failed_attempt_on_fallback_keeps_bounded_fallback(tmp_path: Any) -> None:
    from autoscout.grounded_description import GroundedDescriptionStageService

    state_dir = tmp_path / "state"
    output_dir = tmp_path / "run"
    state_dir.mkdir()
    output_dir.mkdir()
    seed_state(state_dir, candidate_history=[_fallback_record()])
    seed_state(state_dir, candidate_visibility=[])
    write_json(output_dir / "scouting-candidates.json", [_candidate()])
    service = GroundedDescriptionStageService(_SeededStage([_failed_outcome()]))  # type: ignore[arg-type]

    result = service.enrich(output_dir, state_dir, OPERATION_DATE, operation_id="issue-673")

    assert result["status"] == "ok"
    history = read_state(state_dir, "candidate_history")
    assert history[0]["content_description"] == DESCRIPTION_FALLBACK
    assert history[0]["content_description_rejection_reason"] == REASON_FEWER_THAN_TWO


def test_standalone_failed_outcome_preserves_usable_text() -> None:
    from autoscout.generate_descriptions import _apply_outcome

    record = _usable_codex_record()
    _apply_outcome(record, _failed_outcome(), OPERATION_DATE)

    assert record["content_description"] == RICH_TEXT
    assert record["description_provenance"]["provenance"] == "codex-generated"
    assert record["grounded_description_attempts"][-1]["reason_code"] == REASON_FEWER_THAN_TWO


def test_depth_floor_constants_match_policy() -> None:
    assert DEPTH_MIN_CHARS == 200
    assert DEPTH_MIN_SENTENCES == 2


def test_shallow_success_is_rejected_at_artifact_validation() -> None:
    from autoscout.grounded_description import AcceptedSource
    from autoscout.grounded_description import canonicalize_url

    shallow = StageOutcome(
        candidate_id="candidate-001",
        outcome=OUTCOME_SINGLE_SOURCE,
        identity_key="test work|test author",
        content_description="Короткое описание книги.",
        research_sources=(
            AcceptedSource(
                canonical_url=canonicalize_url("https://one.example.test/work"),
                source_kind="publisher",
                source_family="publisher_copy",
                owner_id="owner-one",
                source_language="en",
            ),
        ),
    )
    artifact = StageArtifact(operation_id="issue-673", operation_date=OPERATION_DATE, outcomes=(shallow,), queue=())
    assert validate_stage_artifact(artifact) is False
    assert validate_stage_artifact(artifact.to_dict()) is False


def test_shallow_success_is_not_applied_by_weekly_application() -> None:
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from autoscout.grounded_description import AcceptedSource
    from autoscout.grounded_description import canonicalize_url
    from autoscout.grounded_description_enrichment import _apply_outcomes
    from autoscout.grounded_description_enrichment import _load_inputs
    from autoscout.grounded_description_enrichment import _StageExecution

    with TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        state_dir = tmp_path / "state"
        output_dir = tmp_path / "run"
        state_dir.mkdir()
        output_dir.mkdir()
        seed_state(state_dir, candidate_history=[_fallback_record()])
        seed_state(state_dir, candidate_visibility=[])
        write_json(
            output_dir / "scouting-candidates.json",
            [
                {
                    "candidate_id": "candidate-001",
                    "identity_key": "test work|test author",
                    "title": "Test Work",
                    "primary_authors": ["Test Author"],
                }
            ],
        )
        loaded = _load_inputs(output_dir, state_dir, OPERATION_DATE, "issue-673")
        assert not isinstance(loaded, dict) and loaded is not None
        shallow = StageOutcome(
            candidate_id="candidate-001",
            outcome=OUTCOME_SINGLE_SOURCE,
            identity_key="test work|test author",
            content_description="Короткое описание книги.",
            research_sources=(
                AcceptedSource(
                    canonical_url=canonicalize_url("https://one.example.test/work"),
                    source_kind="publisher",
                    source_family="publisher_copy",
                    owner_id="owner-one",
                    source_language="en",
                ),
            ),
        )
        from autoscout.grounded_description_enrichment import _StageExecution

        applied = _apply_outcomes(
            loaded,
            loaded.current_candidates,
            loaded.current_candidates,
            _StageExecution(
                StageArtifact(operation_id="issue-673", operation_date=OPERATION_DATE, outcomes=(shallow,), queue=()),
                "ok",
            ),
            {},
        )
        assert applied.applied_ids == set()
        assert applied.failed_ids == {"candidate-001"}
        history = applied.history
        assert history[0]["content_description"] == DESCRIPTION_FALLBACK
        assert history[0]["content_description_rejection_reason"] == REASON_INSUFFICIENT_DEPTH


def test_shallow_success_is_not_applied_by_standalone_application() -> None:
    from autoscout.generate_descriptions import _apply_outcome
    from autoscout.grounded_description import AcceptedSource
    from autoscout.grounded_description import canonicalize_url

    record = _fallback_record()
    shallow = StageOutcome(
        candidate_id="candidate-001",
        outcome=OUTCOME_SINGLE_SOURCE,
        identity_key="test work|test author",
        content_description="Короткое описание книги.",
        research_sources=(
            AcceptedSource(
                canonical_url=canonicalize_url("https://one.example.test/work"),
                source_kind="publisher",
                source_family="publisher_copy",
                owner_id="owner-one",
                source_language="en",
            ),
        ),
    )
    _apply_outcome(record, shallow, OPERATION_DATE, operation_id="issue-673")

    assert record["content_description"] == DESCRIPTION_FALLBACK
    assert "grounded_outcome" not in record
    assert record["grounded_description_attempts"][-1]["reason_code"] == REASON_INSUFFICIENT_DEPTH
