"""Offline coverage for issue #641: report the adjudication stage distinctly."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from adjudication_test_support import citation as _citation
from adjudication_test_support import fetcher as _fetcher_for_pages
from adjudication_test_support import page as _page
from adjudication_test_support import response as _response
from state_store_support import seed_state

from autoscout.description_adjudication import ADJUDICATION_DECISION_CLASSIFICATION
from autoscout.description_adjudication import ADJUDICATION_STAGE_ARTIFACT_NAME
from autoscout.description_adjudication import DECISION_APPLY_SINGLE_SOURCE
from autoscout.description_adjudication import DECISION_APPLY_TWO_SOURCE
from autoscout.description_adjudication import DECISION_EVIDENCE_UNCHANGED
from autoscout.description_adjudication import DECISION_FALLBACK
from autoscout.description_adjudication import DECISION_INVALID
from autoscout.description_adjudication import DECISION_NOT_ATTEMPTED
from autoscout.description_adjudication import DECISION_SKIPPED
from autoscout.description_adjudication import DescriptionAdjudicationStageService
from autoscout.description_adjudication import ReplayDescriptionAdjudicationAdapter
from autoscout.description_adjudication import adjudicate_descriptions
from autoscout.description_adjudication import adjudication_reason_label
from autoscout.description_adjudication import build_adjudication_stage_report
from autoscout.grounded_description import REASON_NOT_ATTEMPTED_STAGE_BUDGET
from autoscout.grounded_description import BoundedHTTPError
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import NoNetworkHTTPTransport
from autoscout.grounded_description import canonicalize_url
from autoscout.grounded_description import reason_label

OPERATION_DATE = "2026-09-14"
VALID_TEXT = (
    "Книга о памяти, обществе и общественной жизни маленького города. "
    "Автор соединяет репортаж с историческим контекстом и показывает, "
    "как прошлое формирует настоящее. "
    "Редакционный крючок — тихий повседневный ракурс вместо громких обобщений."
)
PUBLISHER_URL = "https://publisher-example.test/test-book"
CATALOGUE_URL = "https://catalog-example.test/test-book"


def _two_source() -> dict[str, dict[str, Any]]:
    return {
        "test book|test author": _response(
            "test book|test author",
            [
                _citation(
                    PUBLISHER_URL, "publisher-example.test", "publisher", title="Test Book", authors=["Test Author"]
                ),
                _citation(
                    CATALOGUE_URL,
                    "catalog-example.test",
                    "library_catalogue",
                    title="Test Book",
                    authors=["Test Author"],
                ),
            ],
            text=VALID_TEXT,
        )
    }


def _two_fetcher():
    pages = {
        canonicalize_url(PUBLISHER_URL): _page(
            title="Test Book", author="Test Author", subject="A novel about memory, society and public life."
        ),
        canonicalize_url(CATALOGUE_URL): _page(
            title="Test Book", author="Test Author", subject="A novel about memory, society and public life."
        ),
    }
    return _fetcher_for_pages(pages)


def _write_state(tmp_path: Path, records: list[dict[str, Any]]) -> tuple[Path, Path]:
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "run"
    state_dir.mkdir(exist_ok=True)
    output_dir.mkdir(exist_ok=True)
    seed_state(state_dir, candidate_history=records)
    seed_state(state_dir, candidate_visibility=[])
    return state_dir, output_dir


def _record(candidate_id: str, identity_key: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "identity_key": identity_key,
        "title": "Test Book",
        "primary_authors": ["Test Author"],
        "content_description": "",
    }


def test_clean_stage_writes_report_with_semantics(tmp_path: Path) -> None:
    state_dir, output_dir = _write_state(tmp_path, [_record("candidate-001", "test book|test author")])
    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        effective_date=OPERATION_DATE,
        adapter=ReplayDescriptionAdjudicationAdapter(_two_source()),
        fetcher=_two_fetcher(),
        apply=True,
        operation_id="op-123",
    )
    report_path = output_dir / ADJUDICATION_STAGE_ARTIFACT_NAME
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["version"] == 1
    assert report["operation_id"] == "op-123"
    assert report["operation_date"] == OPERATION_DATE
    assert report["status"] == "ok"
    assert report["count_semantics"]["classification_counts"] == ["applied", "skipped", "failed", "ambiguous"]
    assert report["count_semantics"]["overlapping_final_state_counts"] == ["still_fallback"]
    assert "adjudication_decision_counts" in report["count_semantics"]
    # The policy proposal counts were retired with the proposal flow (#1123).
    assert "policy_counts" not in report["count_semantics"]
    assert report["counts"]["applied"] == 1
    assert "proposed_host_policies" not in report["counts"]
    assert "pending_host_policies" not in report
    assert "adjudication_counts" in report
    assert report["adjudication_counts"]["apply_two_source"] == 1
    (record,) = report["records"]
    for field in (
        "candidate_id",
        "identity_key",
        "result",
        "final_state",
        "reason_code",
        "citation_count",
        "owner_count",
        "confidence",
    ):
        assert field in record, f"missing {field}"
    assert record["candidate_id"] == "candidate-001"
    assert record["result"] == "applied"
    assert record["citation_count"] == 2
    assert record["owner_count"] == 2
    assert record["confidence"] == "high"
    assert "budget" in report
    for field in ("stage_budget_seconds", "call_timeout_seconds", "max_workers", "candidate_limit"):
        assert field in report["budget"]
    assert result["description_adjudication_stage_report"] == report


def test_counts_obey_declared_semantics(tmp_path: Path) -> None:
    state_dir, output_dir = _write_state(tmp_path, [_record("candidate-001", "test book|test author")])
    adjudicate_descriptions(
        state_dir,
        output_dir,
        effective_date=OPERATION_DATE,
        adapter=ReplayDescriptionAdjudicationAdapter(_two_source()),
        fetcher=_two_fetcher(),
        apply=True,
    )
    report = json.loads((output_dir / ADJUDICATION_STAGE_ARTIFACT_NAME).read_text(encoding="utf-8"))
    counts = report["counts"]
    total_classified = counts["applied"] + counts["skipped"] + counts["failed"] + counts["ambiguous"]
    assert total_classified == len(report["records"])
    # still_fallback overlaps and never adds to the total.
    assert counts["still_fallback"] <= len(report["records"])


def test_seven_decisions_map_to_documented_classification() -> None:
    assert ADJUDICATION_DECISION_CLASSIFICATION[DECISION_APPLY_TWO_SOURCE] == "applied"
    assert ADJUDICATION_DECISION_CLASSIFICATION[DECISION_APPLY_SINGLE_SOURCE] == "applied"
    assert ADJUDICATION_DECISION_CLASSIFICATION[DECISION_INVALID] == "failed"
    assert ADJUDICATION_DECISION_CLASSIFICATION[DECISION_FALLBACK] == "ambiguous"
    assert ADJUDICATION_DECISION_CLASSIFICATION[DECISION_SKIPPED] == "skipped"
    assert ADJUDICATION_DECISION_CLASSIFICATION[DECISION_EVIDENCE_UNCHANGED] == "skipped"
    assert ADJUDICATION_DECISION_CLASSIFICATION[DECISION_NOT_ATTEMPTED] == "skipped"
    rows = [
        {
            "candidate_id": f"candidate-{i:03d}",
            "identity_key": f"key-{i}",
            "decision": decision,
            "decision_reason": "",
            "response": {},
            "verification": {},
        }
        for i, decision in enumerate(
            [
                DECISION_APPLY_TWO_SOURCE,
                DECISION_APPLY_SINGLE_SOURCE,
                DECISION_FALLBACK,
                DECISION_INVALID,
                DECISION_SKIPPED,
                DECISION_EVIDENCE_UNCHANGED,
                DECISION_NOT_ATTEMPTED,
            ]
        )
    ]
    report = build_adjudication_stage_report(
        operation_id="op",
        operation_date=OPERATION_DATE,
        status="ok",
        rows=rows,
        history=[],
    )
    assert report["counts"]["applied"] == 2
    assert report["counts"]["failed"] == 1
    assert report["counts"]["ambiguous"] == 1
    assert report["counts"]["skipped"] == 3
    assert report["adjudication_counts"]["apply_two_source"] == 1
    assert report["adjudication_counts"]["not_attempted"] == 1


def test_no_adapter_prose_leaves_preview(tmp_path: Path) -> None:
    prose = "UNIQUE_ADAPTER_PROSE_641_XYZ"
    state_dir, output_dir = _write_state(tmp_path, [_record("candidate-001", "test book|test author")])
    adapter = ReplayDescriptionAdjudicationAdapter(
        {
            "test book|test author": _response(
                "test book|test author", [], resolved=False, unresolved_reason=prose, text="", confidence="low"
            )
        }
    )

    class _DeadTransport:
        def request(self, url: str, **_kwargs: Any) -> Any:
            raise BoundedHTTPError("offline")

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        effective_date=OPERATION_DATE,
        adapter=adapter,
        fetcher=BoundedHTTPFetcher(transport=_DeadTransport()),  # type: ignore[arg-type]
        apply=True,
        operation_id="op-prose",
    )
    preview = json.loads((output_dir / "description-adjudication-preview.json").read_text(encoding="utf-8"))
    assert prose in json.dumps(preview)
    report = json.loads((output_dir / ADJUDICATION_STAGE_ARTIFACT_NAME).read_text(encoding="utf-8"))
    assert prose not in json.dumps(report)
    assert prose not in json.dumps(result["description_adjudication_stage_report"])
    # Run summary and narrative must also stay clean; covered end to end below.


def test_every_emitted_reason_has_non_generic_russian_label() -> None:
    from autoscout.description_adjudication import REASON_AUTHOR_UNVERIFIABLE

    assert reason_label(REASON_AUTHOR_UNVERIFIABLE) == "Автор источника не указан"
    assert reason_label(REASON_AUTHOR_UNVERIFIABLE) != "Не удалось подтвердить описание"
    # The adjudication stage clock needs its own true sentence.
    assert adjudication_reason_label(REASON_NOT_ATTEMPTED_STAGE_BUDGET) != reason_label(
        REASON_NOT_ATTEMPTED_STAGE_BUDGET
    )
    assert (
        adjudication_reason_label(REASON_NOT_ATTEMPTED_STAGE_BUDGET) == "Проверка описаний агентом еще не запускалась"
    )
    for code in (
        "ambiguity",
        "fewer_than_two_agreeing_sources",
        "invalid_description",
        "evidence_unchanged",
        "author_unverifiable",
        "title_mismatch",
        "author_mismatch",
        "invalid_source",
        "search_or_fetch_failure",
        "stage_execution_failure",
    ):
        assert adjudication_reason_label(code) != "Не удалось подтвердить описание", code


def test_artifacts_are_registered() -> None:
    from autoscout.safe_artifacts import PROTECTED_BASENAMES
    from autoscout.weekly_report import WEEKLY_REPORT_ARTIFACTS

    names = {artifact.name for artifact in WEEKLY_REPORT_ARTIFACTS}
    assert "description-adjudication-stage.json" in names
    assert "description-adjudication-preview.json" in names
    assert "description-adjudication-stage.json" in PROTECTED_BASENAMES
    assert "description-adjudication-preview.json" in PROTECTED_BASENAMES
    assert "grounded-source-policy-proposals.json" not in PROTECTED_BASENAMES


def _write_codex_fixture_config(tmp_path: Path) -> Path:
    import json as _json

    config_path = tmp_path / "config.json"
    config = {
        "run_date": "2026-07-13",
        "readiness_purpose": "recurring-weekly",
        "editor_row_target": 100,
        "pilot_row_min": 100,
        "pilot_row_max": 100,
        "pilot_mode": True,
        "sources": [
            {
                "name": "Test Source",
                "family": "award",
                "url": "https://example.test/source",
                "access_status": "open",
                "extraction_method": "codex-extraction-replay",
                "check_frequency": "weekly",
                "language": "en",
                "source_page_fixture_file": "test-capture.json",
                "extraction_response_file": "test-extraction.json",
            }
        ],
    }
    config_path.write_text(_json.dumps(config), encoding="utf-8")
    return config_path


def _write_capture_fixture(tmp_path: Path) -> None:
    import json as _json

    (tmp_path / "test-capture.json").write_text(
        _json.dumps(
            {
                "source_url": "https://example.test/source",
                "html": '<html><body><a href="https://example.test/book">Test</a></body></html> by Test Author',
            }
        ),
        encoding="utf-8",
    )


def _write_extraction_fixture(output_dir: Path) -> None:
    worklist = json.loads((output_dir / "codex-extraction-worklist.json").read_text(encoding="utf-8"))
    item = worklist["work_items"][0]
    result = output_dir / str(item["expected_extraction_file"])
    result.write_text(
        json.dumps(
            {
                "source_name": "Test Source",
                "source_url": "https://example.test/source",
                "operation_id": item["operation_id"],
                "capture_sha256": item["capture_sha256"],
                "entries": [
                    {
                        "title": "Test Book",
                        "primary_authors": ["Test Author"],
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


def _run_fixture_weekly(tmp_path: Path, adapters: Any = None) -> tuple[Path, Any]:
    from autoscout.weekly_workflow import deterministic_adapters
    from autoscout.weekly_workflow import run_weekly_workflow

    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = _write_codex_fixture_config(tmp_path)
    output_dir = tmp_path / "run"
    output_dir.mkdir(exist_ok=True)
    _write_capture_fixture(tmp_path)
    active = adapters if adapters is not None else deterministic_adapters()
    active.capture.prepare(config_path, output_dir, live=False)
    _write_extraction_fixture(output_dir)
    result = run_weekly_workflow(config_path, output_dir, adapters=active)
    return output_dir, result


def test_per_candidate_failure_is_warning_and_run_completes(tmp_path: Path) -> None:
    from autoscout.weekly_workflow import deterministic_adapters

    class _BoomAdapter:
        def adjudicate(self, work_item: Any) -> Any:
            raise RuntimeError("per-candidate boom")

    adapters = deterministic_adapters()
    adapters.adjudication = DescriptionAdjudicationStageService(
        adapter=_BoomAdapter(),  # type: ignore[arg-type]
        fetcher=BoundedHTTPFetcher(transport=NoNetworkHTTPTransport()),
    )
    with tempfile.TemporaryDirectory() as tmp:
        output_dir, result = _run_fixture_weekly(Path(tmp), adapters=adapters)
        stages = {entry["stage"]: entry["status"] for entry in result.log}
        # An ordinary per-candidate failure stays a bounded invalid row: the
        # stage itself reports ok, the run continues, and the problem is a
        # warning labelled by stage.
        assert stages.get("description-adjudication") == "ok"
        assert stages.get("review") == "ok"
        assert stages.get("health") == "ok"
        assert result.outcome.value.startswith("completed")
        assert any(problem.get("stage") == "adjudication" for problem in result.description_stage_problems)
        report = result.description_adjudication_stage_report
        assert report["counts"]["failed"] >= 1


def test_persistence_failure_stops_at_named_stage_with_own_sentence(tmp_path: Path) -> None:
    from autoscout.russian_workflow import intervention_message
    from autoscout.weekly_workflow import deterministic_adapters

    adapters = deterministic_adapters()
    real_service = adapters.adjudication
    assert real_service is not None

    class _PersistenceBoom:
        def adjudicate_stage(
            self,
            output_dir: Path,
            state_dir: Path | None = None,
            effective_date: str | None = None,
            *,
            operation_id: str | None = None,
            state_store: Any = None,
        ) -> dict[str, Any]:
            return {
                "status": "persistence_failed",
                "reason": "description_adjudication_persistence_failure",
                "description_adjudication_stage_report": {"status": "persistence_failed", "counts": {}, "records": []},
            }

    adapters.adjudication = _PersistenceBoom()  # type: ignore[assignment]
    with tempfile.TemporaryDirectory() as tmp:
        _output_dir, result = _run_fixture_weekly(Path(tmp), adapters=adapters)
        assert result.failed_stage == "description-adjudication-persistence"
        msg = intervention_message(result.failed_stage or "")
        assert "проверки описаний" in msg
        assert "Возникла непредвиденная ошибка" not in msg


def test_three_failure_kinds_are_distinguishable() -> None:
    from autoscout.russian_workflow import intervention_message
    from autoscout.russian_workflow import render_workflow_result
    from autoscout.weekly_workflow import deterministic_adapters

    capture_msg = intervention_message("capture")
    grounded_msg = intervention_message("description-persistence")
    adjudication_msg = intervention_message("description-adjudication-persistence")
    assert capture_msg != grounded_msg
    assert grounded_msg != adjudication_msg
    assert capture_msg != adjudication_msg
    assert "Возникла непредвиденная ошибка" not in adjudication_msg

    # One run each, end to end: a source-collection failure stops at
    # capture, an adjudication stage failure warns and completes, and an
    # adjudication persistence failure stops at its own named stage.
    class _FailingCapture:
        def prepare(
            self, config_path: Path, output_dir: Path, *, live: bool = False, effective_date: str | None = None
        ) -> dict[str, Any]:
            return {"returncode": 1, "reason": "capture boom"}

    class _StageBoom:
        def adjudicate_stage(
            self,
            output_dir: Path,
            state_dir: Path | None = None,
            effective_date: str | None = None,
            *,
            operation_id: str | None = None,
            state_store: Any = None,
        ) -> dict[str, Any]:
            raise RuntimeError("stage boom")

    class _PersistenceBoom:
        def adjudicate_stage(
            self,
            output_dir: Path,
            state_dir: Path | None = None,
            effective_date: str | None = None,
            *,
            operation_id: str | None = None,
            state_store: Any = None,
        ) -> dict[str, Any]:
            return {
                "status": "persistence_failed",
                "reason": "description_adjudication_persistence_failure",
                "description_adjudication_stage_report": {"status": "persistence_failed", "counts": {}, "records": []},
            }

    with tempfile.TemporaryDirectory() as tmp:
        from autoscout.weekly_workflow import run_weekly_workflow

        base = Path(tmp) / "a"
        base.mkdir(parents=True, exist_ok=True)
        config_path = _write_codex_fixture_config(base)
        output_dir = base / "run"
        output_dir.mkdir(exist_ok=True)
        adapters = deterministic_adapters()
        adapters.capture = _FailingCapture()  # type: ignore[assignment]
        capture_result = run_weekly_workflow(config_path, output_dir, adapters=adapters)
        assert capture_result.failed_stage == "capture"

    with tempfile.TemporaryDirectory() as tmp:
        adapters = deterministic_adapters()
        adapters.adjudication = _StageBoom()  # type: ignore[assignment]
        _out, stage_result = _run_fixture_weekly(Path(tmp) / "b", adapters=adapters)
        assert stage_result.failed_stage in (None, "")
        assert stage_result.outcome.value.startswith("completed")

    with tempfile.TemporaryDirectory() as tmp:
        adapters = deterministic_adapters()
        adapters.adjudication = _PersistenceBoom()  # type: ignore[assignment]
        _out, persist_result = _run_fixture_weekly(Path(tmp) / "c", adapters=adapters)
        assert persist_result.failed_stage == "description-adjudication-persistence"

    assert capture_result.failed_stage != persist_result.failed_stage
    assert render_workflow_result(capture_result) != render_workflow_result(persist_result)
    assert render_workflow_result(stage_result) != render_workflow_result(persist_result)


def test_both_stages_failing_counts_candidate_once() -> None:
    from autoscout.russian_workflow import _deduplicated_description_failures as deduplicated_description_failures
    from autoscout.russian_workflow import completion_message

    grounded_report = {
        "counts": {"failed": 1, "ambiguous": 0},
        "records": [{"candidate_id": "candidate-001", "identity_key": "k1", "result": "failed"}],
    }
    adjudication_report = {
        "counts": {"failed": 1, "ambiguous": 0},
        "records": [{"candidate_id": "candidate-001", "identity_key": "k1", "result": "failed"}],
    }
    assert deduplicated_description_failures(grounded_report, adjudication_report, 2) == 1
    msg = completion_message(
        outcome="completed/attention-required",
        attention_required=True,
        description_stage_failures=2,
        description_stage_report=grounded_report,
        adjudication_stage_report=adjudication_report,
    )
    assert "для 1 книги" in msg


def test_coverage_and_review_agree_after_apply() -> None:
    from autoscout.description_adjudication import ADJUDICATION_MAX_WORKERS
    from autoscout.description_adjudication import ADJUDICATION_STAGE_BUDGET_SECONDS
    from autoscout.weekly_workflow import ADJUDICATION_CANDIDATE_LIMIT
    from autoscout.weekly_workflow import deterministic_adapters

    adapters = deterministic_adapters()
    adapters.adjudication = DescriptionAdjudicationStageService(
        adapter=ReplayDescriptionAdjudicationAdapter(_two_source()),
        fetcher=_two_fetcher(),
        limit=ADJUDICATION_CANDIDATE_LIMIT,
        stage_budget_seconds=ADJUDICATION_STAGE_BUDGET_SECONDS,
        max_workers=ADJUDICATION_MAX_WORKERS,
    )
    with tempfile.TemporaryDirectory() as tmp:
        output_dir, result = _run_fixture_weekly(Path(tmp), adapters=adapters)
        coverage = json.loads((output_dir / "description-coverage.json").read_text(encoding="utf-8"))
        review_html = (output_dir / "weekly-review.html").read_text(encoding="utf-8")
        assert VALID_TEXT in review_html
        assert int(coverage.get("validated_count", 0)) >= 1
        summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
        assert summary["description_coverage"] == coverage
        assert "description_adjudication_stage_report" in summary
        assert "adjudication_proposed_host_policies" not in summary
        # A run summary written before #1123 carries the retired policy
        # proposal counts, and it must still load.
        from autoscout.run_health import run_health

        healthy = run_health(output_dir)
        summary["adjudication_proposed_host_policies"] = 3
        summary["description_adjudication_stage_report"]["counts"]["proposed_host_policies"] = 3
        summary["description_adjudication_stage_report"]["pending_host_policies"] = 539
        summary["description_adjudication_stage_report"]["count_semantics"]["policy_counts"] = [
            "proposed_host_policies"
        ]
        (output_dir / "run-summary.json").write_text(json.dumps(summary), encoding="utf-8")
        assert run_health(output_dir) == healthy
        # Issue #671 changed the rule this comment used to encode: the stage
        # now projects applied history descriptions onto the current/latest
        # snapshots, so the snapshot carries the same text the review
        # renders and coverage counts. The review still merges history
        # first, so all three agree by construction.
        history = json.loads((output_dir / "candidate-history.json").read_text(encoding="utf-8"))
        assert any(record.get("content_description") == VALID_TEXT for record in history)
        snapshot = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
        assert any(VALID_TEXT in str(candidate.get("content_description", "")) for candidate in snapshot), (
            "adjudication must project applied descriptions onto scouting-candidates.json (issue #671)"
        )


def test_russian_narrative_shows_adjudication_line_and_hides_prose() -> None:
    from autoscout.russian_workflow import completion_message

    prose = "UNIQUE_ADAPTER_PROSE_641_NARRATIVE"
    adjudication_report = {
        "counts": {
            "applied": 1,
            "skipped": 0,
            "failed": 0,
            "ambiguous": 1,
            "still_fallback": 1,
            "proposed_host_policies": 1,
        },
        "records": [
            {
                "candidate_id": "candidate-001",
                "identity_key": "k1",
                "result": "applied",
                "final_state": "description_present",
                "reason_code": "",
                "citation_count": 2,
                "owner_count": 2,
                "confidence": "high",
            },
            {
                "candidate_id": "candidate-002",
                "identity_key": "k2",
                "result": "ambiguous",
                "final_state": "still_fallback",
                "reason_code": "ambiguity",
                "citation_count": 0,
                "owner_count": 0,
                "confidence": "low",
            },
        ],
    }
    msg = completion_message(outcome="completed/no-attention", adjudication_stage_report=adjudication_report)
    assert "Проверка описаний агентом" in msg
    assert prose not in msg


def test_two_fixture_runs_keep_stage_report_identical() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        dir_a = base / "a"
        dir_b = base / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        output_a, _ = _run_fixture_weekly(dir_a)
        output_b, _ = _run_fixture_weekly(dir_b)
        for name in (
            "description-adjudication-preview.json",
            "description-adjudication-stage.json",
            "candidate-history.json",
        ):
            assert (output_a / name).read_bytes() == (output_b / name).read_bytes(), f"{name} differs"


# ── Review findings on the first #641 implementation ──────────────────


def test_repaired_candidate_is_not_reported_as_a_failure() -> None:
    """A grounded failure that adjudication applied is not a run failure.

    This is the epic's success path, and the first implementation counted
    it as a failure: the editor read full coverage, an agent line saying
    nothing needed a decision, and a sentence naming one book that failed,
    in three consecutive lines.
    """
    from autoscout.description_adjudication import unresolved_description_candidates
    from autoscout.russian_workflow import completion_message

    grounded_report = {
        "counts": {"failed": 1, "ambiguous": 0},
        "records": [
            {"candidate_id": "candidate-001", "identity_key": "k1", "result": "failed", "final_state": "still_fallback"}
        ],
        "attention_required": True,
    }
    adjudication_report = {
        "counts": {"applied": 1, "failed": 0, "ambiguous": 0},
        "records": [
            {
                "candidate_id": "candidate-001",
                "identity_key": "k1",
                "result": "applied",
                "final_state": "description_present",
            }
        ],
    }
    assert unresolved_description_candidates(grounded_report, adjudication_report) == set()
    message = completion_message(
        outcome="completed/attention-required",
        attention_required=True,
        description_stage_failures=1,
        description_stage_report=grounded_report,
        adjudication_stage_report=adjudication_report,
    )
    assert "Не удалось подготовить описания" not in message


def test_repaired_run_does_not_carry_the_grounded_attention_forward() -> None:
    """The grounded verdict is decided before adjudication can repair it."""
    from autoscout.weekly_workflow import _grounded_attention_survives_adjudication

    grounded_report = {
        "attention_required": True,
        "records": [
            {"candidate_id": "candidate-001", "identity_key": "k1", "result": "failed", "final_state": "still_fallback"}
        ],
    }
    repaired = {
        "records": [
            {
                "candidate_id": "candidate-001",
                "identity_key": "k1",
                "result": "applied",
                "final_state": "description_present",
            }
        ]
    }
    unrepaired = {
        "records": [
            {
                "candidate_id": "candidate-001",
                "identity_key": "k1",
                "result": "ambiguous",
                "final_state": "still_fallback",
            }
        ]
    }
    assert _grounded_attention_survives_adjudication(grounded_report, repaired) is False
    assert _grounded_attention_survives_adjudication(grounded_report, unrepaired) is True
    # A stage that produced no records repaired nothing.
    assert _grounded_attention_survives_adjudication(grounded_report, {}) is True


def test_declined_backlog_candidate_never_forces_attention() -> None:
    """Issue #557: a standing backlog is not a this-run attention fact.

    This stage reads every record that already lacks a description, so a
    decline is the backlog staying the backlog. A schema-invalid response
    is different: that is the stage itself misbehaving.
    """
    declined = build_adjudication_stage_report(
        rows=[
            {
                "candidate_id": "candidate-001",
                "identity_key": "k1",
                "decision": DECISION_FALLBACK,
                "decision_reason": "ambiguity",
                "verification": {},
                "response": {},
            }
        ],
        history=[{"identity_key": "k1", "content_description": ""}],
        operation_id="op",
        operation_date=OPERATION_DATE,
        status="ok",
    )
    assert declined["counts"]["ambiguous"] == 1
    assert declined["attention_required"] is False

    invalid = build_adjudication_stage_report(
        rows=[
            {
                "candidate_id": "candidate-001",
                "identity_key": "k1",
                "decision": DECISION_INVALID,
                "decision_reason": "schema invalid",
                "verification": {},
                "response": {},
            }
        ],
        history=[{"identity_key": "k1", "content_description": ""}],
        operation_id="op",
        operation_date=OPERATION_DATE,
        status="ok",
    )
    assert invalid["counts"]["failed"] == 1
    assert invalid["attention_required"] is True


def test_report_artifact_write_failure_is_a_warning_not_a_lost_review() -> None:
    """A diagnostic write failure must not claim state was not saved.

    The two state writes are the persistence boundary. The preview and the
    stage report are written after state is already durable, so a failure
    there leaves nothing inconsistent. The first implementation called it a
    persistence failure, which threw away the weekly review and told the
    editor to repeat about an hour of agent work.
    """
    import autoscout.description_adjudication as adjudication_module
    from autoscout.weekly_workflow import ADJUDICATION_CANDIDATE_LIMIT
    from autoscout.weekly_workflow import deterministic_adapters

    real_write = adjudication_module.write_json

    def _fail_the_preview_only(path: Path, data: Any) -> None:
        if Path(path).name == "description-adjudication-preview.json":
            raise OSError("no space left on device")
        real_write(path, data)

    adapters = deterministic_adapters()
    adapters.adjudication = DescriptionAdjudicationStageService(
        adapter=ReplayDescriptionAdjudicationAdapter(_two_source()),
        fetcher=_two_fetcher(),
        limit=ADJUDICATION_CANDIDATE_LIMIT,
    )
    adjudication_module.write_json = _fail_the_preview_only  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir, result = _run_fixture_weekly(Path(tmp), adapters=adapters)
            history = json.loads((output_dir / "candidate-history.json").read_text(encoding="utf-8"))
    finally:
        adjudication_module.write_json = real_write

    # State was written, so the run must not say otherwise.
    assert history[0]["description_provenance"]["provenance"] == "agent-adjudicated"
    assert result.failed_stage != "description-adjudication-persistence"
    stages = {entry["stage"]: entry["status"] for entry in result.log}
    assert stages.get("description-adjudication") == "failed"
    assert stages.get("review") == "ok"
    assert stages.get("health") == "ok"
