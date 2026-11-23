"""Offline coverage for issue #671: shared description projection.

Candidate History is authoritative for Content Descriptions. Every route
that writes descriptions (grounded enrichment, adjudication, standalone
generation, legacy enrichment) must project the same description facts
onto the current/latest Scouting Candidates without changing membership
or unrelated fields.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from adjudication_test_support import citation as _citation
from adjudication_test_support import fetcher as _fetcher_for_pages
from adjudication_test_support import page as _page
from adjudication_test_support import response as _response
from state_store_support import read_state
from state_store_support import seed_state

from autoscout.description_projection import DESCRIPTION_PROJECTION_FIELDS
from autoscout.description_projection import project_history_descriptions
from autoscout.grounded_description import OUTCOME_SINGLE_SOURCE
from autoscout.grounded_description import AcceptedSource
from autoscout.grounded_description import GroundedDescriptionStageService
from autoscout.grounded_description import StageArtifact
from autoscout.grounded_description import StageOutcome
from autoscout.grounded_description import canonicalize_url
from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.state_store import open_state_store

OPERATION_DATE = "2026-09-14"
RICH_TEXT = (
    "Книга о памяти, обществе и общественной жизни маленького города. "
    "Автор соединяет репортаж с историческим контекстом и показывает, "
    "как прошлое формирует настоящее. "
    "Редакционный крючок — тихий повседневный ракурс вместо громких обобщений."
)
PUBLISHER_URL = "https://publisher.example.test/test-book"


def _provenance(name: str, text: str) -> dict[str, Any]:
    return {
        "identity_key": "work one|alex author",
        "content_description": text,
        "provenance": name,
        "applied_date": OPERATION_DATE,
        "evidence_basis": [PUBLISHER_URL],
    }


def _history_record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "candidate_id": "candidate-001",
        "identity_key": "work one|alex author",
        "title": "Work One",
        "primary_authors": ["Alex Author"],
        "content_description": RICH_TEXT,
        "description_provenance": _provenance("grounded", RICH_TEXT),
        "grounded_outcome": "grounded",
    }
    record.update(overrides)
    return record


def _candidate(**overrides: Any) -> dict[str, Any]:
    candidate: dict[str, Any] = {
        "candidate_id": "candidate-001",
        "identity_key": "work one|alex author",
        "title": "Work One",
        "primary_authors": ["Alex Author"],
        "publisher": "Example Press",
    }
    candidate.update(overrides)
    return candidate


# ── Unit: the shared projection ───────────────────────────────────────


def test_projection_copies_authoritative_fields_and_preserves_the_rest() -> None:
    history = [_history_record()]
    candidates = [_candidate(content_description="stale text", publisher="Kept Press", extra_field="kept")]

    projected, summary = project_history_descriptions(history, candidates, {})

    assert len(projected) == 1
    row = projected[0]
    assert row["content_description"] == RICH_TEXT
    assert row["description_provenance"] == _provenance("grounded", RICH_TEXT)
    assert row["grounded_outcome"] == "grounded"
    assert row["publisher"] == "Kept Press"
    assert row["extra_field"] == "kept"
    assert summary == {"considered": 1, "projected": 1, "unmatched": 0, "metadata_cleared": 0}
    # The inputs are not mutated: projection returns new rows.
    assert candidates[0]["content_description"] == "stale text"
    assert "description_provenance" not in candidates[0]


def test_projection_clears_stale_metadata_but_never_the_payload() -> None:
    history = [_history_record()]
    del history[0]["grounded_outcome"]
    del history[0]["description_provenance"]
    del history[0]["content_description"]
    candidates = [
        _candidate(
            content_description="editor-visible copy",
            grounded_outcome="grounded",
            description_provenance=_provenance("grounded", "old"),
            content_description_rejection_reason="stale reason",
        )
    ]

    projected, summary = project_history_descriptions(history, candidates, {})

    row = projected[0]
    assert row["content_description"] == "editor-visible copy"
    assert "grounded_outcome" not in row
    assert "description_provenance" not in row
    assert "content_description_rejection_reason" not in row
    assert summary["metadata_cleared"] == 3


def test_projection_overwrites_stale_text_with_authoritative_fallback() -> None:
    history = [
        _history_record(
            content_description="Описание требует дополнительной проверки",
            description_provenance=_provenance("fallback", "Описание требует дополнительной проверки"),
            content_description_rejection_reason="fewer_than_two_agreeing_sources",
        )
    ]
    del history[0]["grounded_outcome"]
    candidates = [_candidate(content_description="obsolete valid text", grounded_outcome="grounded")]

    projected, _summary = project_history_descriptions(history, candidates, {})

    row = projected[0]
    assert row["content_description"] == "Описание требует дополнительной проверки"
    assert row["content_description_rejection_reason"] == "fewer_than_two_agreeing_sources"
    assert "grounded_outcome" not in row


def test_unmatched_rows_pass_through_untouched() -> None:
    history = [_history_record()]
    candidates = [_candidate(), _candidate(candidate_id="candidate-999", identity_key="unknown|nobody")]

    projected, summary = project_history_descriptions(history, candidates, {})

    assert len(projected) == 2
    assert projected[0]["content_description"] == RICH_TEXT
    assert "content_description" not in projected[1]
    assert summary == {"considered": 2, "projected": 1, "unmatched": 1, "metadata_cleared": 0}


def test_projection_follows_alias_redirects() -> None:
    history = [_history_record(candidate_id="candidate-new", identity_key="work one|alex author")]
    candidates = [_candidate(candidate_id="candidate-old", identity_key="work one|retired key")]
    aliases = {"version": 1, "aliases": {"work one|retired key": "candidate-new"}, "candidates": {}}

    projected, summary = project_history_descriptions(history, candidates, aliases)

    # Description facts travel; the row keeps its own identity.
    assert projected[0]["content_description"] == RICH_TEXT
    assert projected[0]["identity_key"] == "work one|retired key"
    assert projected[0]["candidate_id"] == "candidate-old"
    assert summary["projected"] == 1


def test_projection_is_idempotent() -> None:
    history = [_history_record()]
    candidates = [_candidate(content_description="stale")]

    once, _ = project_history_descriptions(history, candidates, {})
    twice, _ = project_history_descriptions(history, once, {})

    assert twice == once


def test_projection_field_set_is_the_documented_contract() -> None:
    assert set(DESCRIPTION_PROJECTION_FIELDS) == {
        "content_description",
        "description_provenance",
        "grounded_outcome",
        "content_description_rejection_reason",
        "grounded_description_attempts",
        # Issue #791: the review status and the coverage count for a book
        # that is not yet published read this date from the snapshot too.
        "description_not_yet_published_date",
        # Issue #947: whether a source other than a PM deal ever cited the
        # book, so coverage never counts it as awaiting publication.
        "cited_beyond_pm_deals",
    }


def test_on_match_hook_runs_and_its_failure_is_loud() -> None:
    seen: list[tuple[str, str]] = []

    def _hook(row: dict[str, Any], matched: Any) -> None:
        seen.append((str(row["identity_key"]), str(matched["identity_key"])))

    projected, _ = project_history_descriptions([_history_record()], [_candidate()], {}, on_match=_hook)
    assert seen == [("work one|alex author", "work one|alex author")]
    assert projected[0]["content_description"] == RICH_TEXT

    def _boom(row: dict[str, Any], matched: Any) -> None:
        raise RuntimeError("hook failed")

    try:
        project_history_descriptions([_history_record()], [_candidate()], {}, on_match=_boom)
    except RuntimeError:
        pass
    else:
        raise AssertionError("hook exceptions must propagate")


# ── Integration: grounded enrichment projects current and latest ──────


class _SeededStage:
    """Replay prebuilt outcomes for exactly the requested candidates."""

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


def _accepted_source() -> AcceptedSource:
    return AcceptedSource(
        canonical_url=canonicalize_url(PUBLISHER_URL),
        source_kind="publisher",
        source_family="publisher_copy",
        owner_id="publisher-supplied-copy",
        source_language="en",
    )


def _grounded_workspace(tmp_path: Path) -> tuple[Path, Path]:
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "run"
    state_dir.mkdir()
    output_dir.mkdir()
    return state_dir, output_dir


def _write_grounded_inputs(
    state_dir: Path,
    output_dir: Path,
    *,
    latest_text: str | None = "stale latest copy",
    latest_provenance: str = "grounded",
) -> None:
    history_record = _history_record()
    del history_record["content_description"]
    del history_record["description_provenance"]
    del history_record["grounded_outcome"]
    seed_state(state_dir, candidate_history=[history_record])
    seed_state(state_dir, candidate_visibility=[])
    write_json(output_dir / "scouting-candidates.json", [_candidate(content_description="stale current copy")])
    if latest_text is not None:
        seed_state(
            state_dir,
            latest_scouting_candidates=[
                _candidate(
                    content_description=latest_text,
                    description_provenance=_provenance(latest_provenance, latest_text),
                    grounded_outcome="grounded",
                    content_description_rejection_reason="stale reason",
                )
            ],
        )


def _single_source_outcome() -> StageOutcome:
    return StageOutcome(
        candidate_id="candidate-001",
        outcome=OUTCOME_SINGLE_SOURCE,
        identity_key="work one|alex author",
        content_description=RICH_TEXT,
        research_sources=(_accepted_source(),),
    )


def test_grounded_enrichment_projects_current_and_latest(tmp_path: Path) -> None:
    state_dir, output_dir = _grounded_workspace(tmp_path)
    _write_grounded_inputs(state_dir, output_dir)
    service = GroundedDescriptionStageService(_SeededStage([_single_source_outcome()]))  # type: ignore[arg-type]

    result = service.enrich(output_dir, state_dir, OPERATION_DATE, operation_id="issue-671")

    assert result["status"] == "ok"
    assert result["grounded"] == 0
    assert result["single_source"] == 1
    for records in (
        read_json(output_dir / "scouting-candidates.json"),
        read_state(state_dir, "latest_scouting_candidates"),
        read_state(state_dir, "candidate_history"),
    ):
        assert records[0]["content_description"] == RICH_TEXT
        assert records[0]["description_provenance"]["provenance"] == "grounded"
    # The stale rejection reason beside the old latest text is gone.
    latest = read_state(state_dir, "latest_scouting_candidates")
    assert "content_description_rejection_reason" not in latest[0]
    report = result["description_stage_report"]
    assert report["projection"]["current"]["projected"] == 1
    assert report["projection"]["latest"]["projected"] == 1


def test_grounded_enrichment_without_latest_snapshot_creates_none(tmp_path: Path) -> None:
    state_dir, output_dir = _grounded_workspace(tmp_path)
    _write_grounded_inputs(state_dir, output_dir, latest_text=None)
    service = GroundedDescriptionStageService(_SeededStage([_single_source_outcome()]))  # type: ignore[arg-type]

    result = service.enrich(output_dir, state_dir, OPERATION_DATE, operation_id="issue-671")

    assert result["status"] == "ok"
    assert not (state_dir / "latest-scouting-candidates.json").exists()
    assert result["description_stage_report"]["projection"]["latest"] is None
    current = read_json(output_dir / "scouting-candidates.json")
    assert current[0]["content_description"] == RICH_TEXT


@pytest.mark.json_backend_only("a malformed state file on disk")
def test_grounded_enrichment_rejects_invalid_latest_snapshot(tmp_path: Path) -> None:
    state_dir, output_dir = _grounded_workspace(tmp_path)
    _write_grounded_inputs(state_dir, output_dir, latest_text=None)
    (state_dir / "latest-scouting-candidates.json").write_text('{"not": "a list"}', encoding="utf-8")
    service = GroundedDescriptionStageService(_SeededStage([_single_source_outcome()]))  # type: ignore[arg-type]

    result = service.enrich(output_dir, state_dir, OPERATION_DATE, operation_id="issue-671")

    assert result["status"] == "input_failed"
    assert result["reason"] == "invalid_latest_scouting_candidates"


def test_grounded_interrupted_publication_recovers_by_rerun(tmp_path: Path) -> None:
    import autoscout.grounded_description_enrichment as enrichment_module

    state_dir, output_dir = _grounded_workspace(tmp_path)
    _write_grounded_inputs(state_dir, output_dir)
    service = GroundedDescriptionStageService(_SeededStage([_single_source_outcome()]))  # type: ignore[arg-type]

    real_publish = enrichment_module.publish_bundle

    def _boom(paths: Any, contents: Any, *, replace: Any = None) -> None:
        raise RuntimeError("interrupted publication")

    enrichment_module.publish_bundle = _boom  # type: ignore[assignment]
    try:
        result = service.enrich(output_dir, state_dir, OPERATION_DATE, operation_id="issue-671")
    finally:
        enrichment_module.publish_bundle = real_publish
    assert result["status"] == "persistence_failed"

    result = service.enrich(output_dir, state_dir, OPERATION_DATE, operation_id="issue-671")

    assert result["status"] == "ok"
    for records in (
        read_json(output_dir / "scouting-candidates.json"),
        read_state(state_dir, "latest_scouting_candidates"),
        read_state(state_dir, "candidate_history"),
    ):
        assert records[0]["content_description"] == RICH_TEXT


# ── Integration: adjudication projects after history is durable ───────


CATALOGUE_URL = "https://catalog.example.test/test-book"


def _canned_two_source() -> dict[str, dict[str, Any]]:
    return {
        "test book|test author": _response(
            "test book|test author",
            [
                _citation(
                    PUBLISHER_URL, "publisher.example.test", "publisher", title="Test Book", authors=["Test Author"]
                ),
                _citation(
                    CATALOGUE_URL,
                    "catalog.example.test",
                    "library_catalogue",
                    title="Test Book",
                    authors=["Test Author"],
                ),
            ],
            text=RICH_TEXT,
        )
    }


def _two_source_fetcher() -> Any:
    subject = "A novel about memory, society and public life."
    pages = {
        canonicalize_url(PUBLISHER_URL): _page(title="Test Book", author="Test Author", subject=subject),
        canonicalize_url(CATALOGUE_URL): _page(title="Test Book", author="Test Author", subject=subject),
    }
    return _fetcher_for_pages(pages)


def _write_adjudication_inputs(state_dir: Path, output_dir: Path) -> None:
    from autoscout.content_description import DESCRIPTION_FALLBACK

    seed_state(
        state_dir,
        candidate_history=[
            {
                "candidate_id": "candidate-001",
                "identity_key": "test book|test author",
                "title": "Test Book",
                "primary_authors": ["Test Author"],
                "content_description": DESCRIPTION_FALLBACK,
            }
        ],
    )
    seed_state(state_dir, candidate_visibility=[])
    write_json(
        output_dir / "scouting-candidates.json",
        [
            {
                "candidate_id": "candidate-001",
                "identity_key": "test book|test author",
                "title": "Test Book",
                "primary_authors": ["Test Author"],
                "publisher": "Kept Press",
                "content_description": "stale snapshot copy",
            }
        ],
    )
    seed_state(
        state_dir,
        latest_scouting_candidates=[
            {
                "candidate_id": "candidate-001",
                "identity_key": "test book|test author",
                "title": "Test Book",
                "primary_authors": ["Test Author"],
                "content_description": "",
            }
        ],
    )


def test_adjudication_apply_projects_current_and_latest(tmp_path: Path) -> None:
    from autoscout.description_adjudication import ReplayDescriptionAdjudicationAdapter
    from autoscout.description_adjudication import adjudicate_descriptions

    state_dir = tmp_path / "state"
    output_dir = tmp_path / "run"
    state_dir.mkdir()
    output_dir.mkdir()
    _write_adjudication_inputs(state_dir, output_dir)

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=ReplayDescriptionAdjudicationAdapter(_canned_two_source()),
        fetcher=_two_source_fetcher(),
        apply=True,
        effective_date=OPERATION_DATE,
        operation_id="issue-671",
    )

    assert result["status"] == "ok"
    assert result["applied"] == 1
    history = read_state(state_dir, "candidate_history")
    assert history[0]["content_description"] == RICH_TEXT
    assert history[0]["description_provenance"]["provenance"] == "agent-adjudicated"
    current = read_json(output_dir / "scouting-candidates.json")
    assert current[0]["content_description"] == RICH_TEXT
    assert current[0]["publisher"] == "Kept Press"
    latest = read_state(state_dir, "latest_scouting_candidates")
    assert latest[0]["content_description"] == RICH_TEXT
    assert result["description_projection"]["current"]["projected"] == 1
    assert result["description_projection"]["latest"]["projected"] == 1


def test_adjudication_preview_projects_nothing(tmp_path: Path) -> None:
    from autoscout.description_adjudication import ReplayDescriptionAdjudicationAdapter
    from autoscout.description_adjudication import adjudicate_descriptions

    state_dir = tmp_path / "state"
    output_dir = tmp_path / "run"
    state_dir.mkdir()
    output_dir.mkdir()
    _write_adjudication_inputs(state_dir, output_dir)

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=ReplayDescriptionAdjudicationAdapter(_canned_two_source()),
        fetcher=_two_source_fetcher(),
        apply=False,
        effective_date=OPERATION_DATE,
        operation_id="issue-671",
    )

    assert result["status"] == "ok"
    assert result["description_projection"] == {"current": None, "latest": None}
    assert read_json(output_dir / "scouting-candidates.json")[0]["content_description"] == "stale snapshot copy"
    assert read_state(state_dir, "latest_scouting_candidates")[0]["content_description"] == ""
    assert "description_provenance" not in read_state(state_dir, "candidate_history")[0]


# ── Integration: standalone generation ────────────────────────────────


def test_standalone_render_candidates_projects_current_and_latest(tmp_path: Path) -> None:
    from autoscout.generate_descriptions import _ensure_render_candidates

    output_dir = tmp_path / "run"
    state_dir = tmp_path / "state"
    output_dir.mkdir()
    state_dir.mkdir()
    history = [_history_record()]
    write_json(
        output_dir / "scouting-candidates.json",
        [_candidate(content_description="stale", grounded_outcome="attempted_no_description")],
    )
    seed_state(state_dir, latest_scouting_candidates=[_candidate(content_description="")])

    summary = _ensure_render_candidates(output_dir, history, aliases={}, store=open_state_store(state_dir))

    assert summary["current"]["projected"] == 1
    assert summary["latest"]["projected"] == 1
    current = read_json(output_dir / "scouting-candidates.json")
    assert current[0]["content_description"] == RICH_TEXT
    # The history record holds no rejection reason, so the stale one is gone.
    assert "content_description_rejection_reason" not in current[0]
    latest = read_state(state_dir, "latest_scouting_candidates")
    assert latest[0]["content_description"] == RICH_TEXT


def test_standalone_render_candidates_projects_latest_without_current(tmp_path: Path) -> None:
    """Review finding on #671: initializing current candidates must not skip latest."""
    from autoscout.generate_descriptions import _ensure_render_candidates

    output_dir = tmp_path / "run"
    state_dir = tmp_path / "state"
    output_dir.mkdir()
    state_dir.mkdir()
    history = [_history_record()]
    seed_state(state_dir, latest_scouting_candidates=[_candidate(content_description="stale latest")])

    summary = _ensure_render_candidates(output_dir, history, aliases={}, store=open_state_store(state_dir))

    assert summary["current"] is None
    assert summary["latest"]["projected"] == 1
    assert read_json(output_dir / "scouting-candidates.json")[0]["content_description"] == RICH_TEXT
    assert read_state(state_dir, "latest_scouting_candidates")[0]["content_description"] == RICH_TEXT


def test_standalone_render_candidates_skips_missing_latest(tmp_path: Path) -> None:
    from autoscout.generate_descriptions import _ensure_render_candidates

    output_dir = tmp_path / "run"
    output_dir.mkdir()
    history = [_history_record()]
    write_json(output_dir / "scouting-candidates.json", [_candidate(content_description="stale")])

    summary = _ensure_render_candidates(output_dir, history)

    assert summary == {
        "current": {"considered": 1, "projected": 1, "unmatched": 0, "metadata_cleared": 0},
        "latest": None,
    }
    assert read_json(output_dir / "scouting-candidates.json")[0]["content_description"] == RICH_TEXT


# ── Integration: legacy enrichment ────────────────────────────────────


def test_legacy_enrichment_projects_candidates_and_existing_latest(tmp_path: Path) -> None:
    from autoscout.description_enrichment import NoOpDescriptionEnrichmentAgent
    from autoscout.description_enrichment import run_enrichment

    output_dir = tmp_path / "run"
    state_dir = tmp_path / "state"
    output_dir.mkdir()
    state_dir.mkdir()
    write_json(
        output_dir / "scouting-candidates.json",
        [_candidate(update_delta="new", content_description="stale")],
    )
    seed_state(state_dir, candidate_history=[_candidate(content_description="")])
    seed_state(state_dir, candidate_visibility=[])
    seed_state(state_dir, latest_scouting_candidates=[_candidate(content_description="older latest copy")])

    result = run_enrichment(
        output_dir, state_dir, agent=NoOpDescriptionEnrichmentAgent(), effective_date=OPERATION_DATE
    )

    assert result["status"] == "ok"
    # The no-op agent returns nothing, so the record keeps the fallback —
    # and the snapshots show exactly that fallback, not their stale texts.
    current = read_json(output_dir / "scouting-candidates.json")
    assert current[0]["content_description"] == "Описание требует дополнительной проверки"
    latest = read_state(state_dir, "latest_scouting_candidates")
    assert len(latest) == 1
    assert latest[0]["content_description"] == "Описание требует дополнительной проверки"


# ── Downstream: coverage consumes the projected snapshots ─────────────


def test_coverage_on_projected_snapshot_matches_history(tmp_path: Path) -> None:
    from autoscout.content_description import description_coverage

    state_dir, output_dir = _grounded_workspace(tmp_path)
    _write_grounded_inputs(state_dir, output_dir)
    service = GroundedDescriptionStageService(_SeededStage([_single_source_outcome()]))  # type: ignore[arg-type]

    result = service.enrich(output_dir, state_dir, OPERATION_DATE, operation_id="issue-671")

    assert result["status"] == "ok"
    history = read_state(state_dir, "candidate_history")
    current = read_json(output_dir / "scouting-candidates.json")
    latest = read_state(state_dir, "latest_scouting_candidates")
    history_coverage = description_coverage(history)
    assert description_coverage(current)["validated_count"] == history_coverage["validated_count"] == 1
    assert description_coverage(latest)["validated_count"] == 1
    assert (
        json.loads((output_dir / "grounded-description-stage.json").read_text(encoding="utf-8"))["counts"][
            "single_source"
        ]
        == 1
    )
