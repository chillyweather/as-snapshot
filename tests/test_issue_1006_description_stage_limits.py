import pytest
from state_store_support import seed_state

from autoscout.description_stage_limits import description_stage_limits


@pytest.mark.parametrize(
    ("count", "seconds", "limit"),
    [
        (None, 900.0, 120),
        (0, 900.0, 120),
        (-1, 900.0, 120),
        (10, 900.0, 120),
        (279, 1116.0, 279),
        (296, 1184.0, 296),
        (1000, 2700.0, 360),
    ],
)
def test_policy(count: int | None, seconds: float, limit: int) -> None:
    result = description_stage_limits(count)
    assert result.grounded_stage_budget_seconds == seconds
    assert result.adjudication_candidate_limit == limit


def test_completion_distinguishes_deferrals_from_failures() -> None:
    from autoscout.russian_workflow import completion_message

    grounded = {
        "records": (
            [
                {"candidate_id": f"g{i}", "result": "skipped", "reason_code": "not_attempted_stage_budget"}
                for i in range(38)
            ]
            + [{"candidate_id": f"d{i}", "result": "failed"} for i in range(85)]
            + [{"candidate_id": f"f{i}", "result": "failed"} for i in range(6)]
        )
    }
    adjudication = {
        "records": [{"candidate_id": f"f{i}", "result": "failed"} for i in range(6)],
        "deferred_limit": {
            "count": 105,
            "candidate_ids": [f"d{i}" for i in range(85)] + [f"old{i}" for i in range(20)],
        },
    }
    message = completion_message(
        "completed/attention-required",
        attention_required=True,
        description_stage_report=grounded,
        adjudication_stage_report=adjudication,
    )
    assert "Отложено из-за лимита времени: составление описаний - 38, проверка описаний - 85." in message
    assert "Не удалось подготовить описания для 6 книг" in message
    grounded["records"] = grounded["records"][:38]
    adjudication["records"] = []
    message = completion_message(
        "completed/attention-required",
        attention_required=True,
        description_stage_report=grounded,
        adjudication_stage_report=adjudication,
    )
    assert "Не удалось подготовить описания" not in message


def test_grounded_weekly_budget_and_forced_budget() -> None:
    from autoscout.grounded_description import GroundedDescriptionStage

    class RecordingStage(GroundedDescriptionStage):
        def _attempt_worklist(self, worklist, deadline, *args, **kwargs):
            self.deadline = deadline
            return []

    candidates = [{"candidate_id": str(i), "update_delta": "new"} for i in range(296)]
    candidates += [{"candidate_id": f"old{i}", "update_delta": "unchanged"} for i in range(500)]
    stage = RecordingStage(monotonic=lambda: 0.0, scale_weekly_budget=True)
    ids = {c["candidate_id"] for c in candidates}
    artifact = stage.run(candidates, operation_id="run", operation_date="2026-01-01", eligible_candidate_ids=ids)
    assert artifact.stage_budget_seconds == stage.deadline == 1184.0
    forced = stage.run(candidates, operation_id="run", operation_date="2026-01-01", force_candidates=True)
    assert forced.stage_budget_seconds == stage.deadline == 900.0
    big = candidates + [{"candidate_id": f"n{i}", "update_delta": "new"} for i in range(1000)]
    artifact = stage.run(big, operation_id="run", operation_date="2026-01-01")
    assert artifact.stage_budget_seconds == stage.deadline == 2700.0


@pytest.mark.parametrize(("count", "expected"), [(279, 279), (1000, 360), (None, 120)])
def test_weekly_adjudication_uses_grounded_report(tmp_path, monkeypatch, count, expected) -> None:
    import json

    import autoscout.description_adjudication as module

    if count is not None:
        (tmp_path / "grounded-description-stage.json").write_text(
            json.dumps({"report": {"operation_id": "run", "lifecycle": {"new_and_updated": {"eligible": count}}}})
        )
    monkeypatch.setattr(module, "adjudicate_descriptions", lambda *args, **kwargs: kwargs)
    service = module.DescriptionAdjudicationStageService(
        adapter=module.ReplayDescriptionAdjudicationAdapter(), scale_weekly_limit=True
    )
    assert service.adjudicate_stage(tmp_path, operation_id="run")["limit"] == expected


@pytest.mark.parametrize(("count", "budget"), [(296, 1184.0), (1000, 2700.0)])
def test_actual_admission_uses_scaled_deadline(count: int, budget: float) -> None:
    from autoscout.grounded_description import REASON_NOT_ATTEMPTED_STAGE_BUDGET
    from autoscout.grounded_description import GroundedDescriptionStage

    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

    class Discovery:
        def __init__(self, clock):
            self.clock = clock
            self.calls = []

        def discover(self, candidate, **kwargs):
            self.calls.append((candidate["candidate_id"], self.clock.now))
            self.clock.now += 10.0
            return []

    clock = Clock()
    discovery = Discovery(clock)
    stage = GroundedDescriptionStage(discovery=discovery, monotonic=clock, max_workers=1, scale_weekly_budget=True)
    candidates = [
        {"candidate_id": str(i), "update_delta": "new", "title": f"Book {i}", "primary_authors": ["Author"]}
        for i in range(count)
    ]
    artifact = stage.run(candidates, operation_id="run", operation_date="2026-01-01")
    assert artifact.stage_budget_seconds == budget
    assert any(start >= 900 for _, start in discovery.calls)
    assert all(start < budget for _, start in discovery.calls)
    assert any(row.reason_code == REASON_NOT_ATTEMPTED_STAGE_BUDGET for row in artifact.outcomes)


def test_explicit_limit_reports_cut_items(tmp_path) -> None:
    from autoscout.description_adjudication import ReplayDescriptionAdjudicationAdapter
    from autoscout.description_adjudication import adjudicate_descriptions

    history = [
        {
            "candidate_id": f"c{i}",
            "identity_key": f"book {i}|author",
            "title": f"Book {i}",
            "primary_authors": ["Author"],
            "content_description": "",
        }
        for i in range(5)
    ]
    seed_state(tmp_path, candidate_history=history, candidate_visibility=[])
    result = adjudicate_descriptions(
        tmp_path,
        tmp_path,
        adapter=ReplayDescriptionAdjudicationAdapter(),
        limit=2,
        stage_budget_seconds=1.0,
        monotonic=lambda: 0.0,
    )
    assert result["previewed"] == 2
    assert result["deferred_limit"] == 3
    deferred = result["description_adjudication_stage_report"]["deferred_limit"]
    assert deferred["count"] == len(deferred["candidate_ids"]) == 3


def test_all_grounded_failures_deferred_without_adjudication_rows_need_no_attention() -> None:
    from autoscout.weekly_workflow import _grounded_attention_survives_adjudication

    grounded = {"records": [{"candidate_id": "book", "result": "failed"}]}
    adjudication = {"records": [], "deferred_limit": {"count": 1, "candidate_ids": ["book"]}}
    assert not _grounded_attention_survives_adjudication(grounded, adjudication)


def test_weekly_progress_snapshot_uses_derived_budget(tmp_path) -> None:
    import json

    from autoscout.grounded_description import GroundedDescriptionStage
    from autoscout.progress_snapshot import CandidateProgressSnapshot

    class EmptyStage(GroundedDescriptionStage):
        def _attempt_worklist(self, worklist, deadline, *args, **kwargs):
            return []

    tracker = CandidateProgressSnapshot(tmp_path, stage="enrichment", stage_budget_seconds=900.0, monotonic=lambda: 0.0)
    stage = EmptyStage(monotonic=lambda: 0.0, scale_weekly_budget=True)
    candidates = [{"candidate_id": str(i), "update_delta": "new"} for i in range(296)]
    stage.run(candidates, operation_id="run", operation_date="2026-01-01", snapshot_tracker=tracker)
    snapshot = json.loads((tmp_path / "progress-snapshot.json").read_text())
    assert snapshot["stage_budget_seconds"] == 1184.0


@pytest.mark.parametrize(
    "resolved", [{"final_state": "description_present"}, {"reason_code": "work_not_yet_published"}]
)
def test_grounded_deferral_resolved_by_adjudication_is_not_counted(resolved) -> None:
    from autoscout.russian_workflow import completion_message

    grounded = {
        "records": [
            {"candidate_id": name, "result": "skipped", "reason_code": "not_attempted_stage_budget"}
            for name in ("A", "B")
        ]
    }
    adjudication = {"records": [{"candidate_id": "A", **resolved}]}
    message = completion_message(
        "completed/no-attention", description_stage_report=grounded, adjudication_stage_report=adjudication
    )
    assert "Отложено из-за лимита времени: составление описаний - 1." in message


@pytest.mark.parametrize("path", ["FEEDBACK_APPLIED", "FEEDBACK_NOOP"])
def test_feedback_only_never_reports_description_deferrals(path) -> None:
    from autoscout.pipeline_path import PipelinePath
    from autoscout.russian_workflow import completion_message

    grounded = {"records": [{"candidate_id": "A", "result": "skipped", "reason_code": "not_attempted_stage_budget"}]}
    message = completion_message(
        "completed/attention-required",
        attention_required=True,
        pipeline_path=getattr(PipelinePath, path),
        description_stage_report=grounded,
    )
    assert "Отложено" not in message
    assert "Часть источников не была проверена" in message
