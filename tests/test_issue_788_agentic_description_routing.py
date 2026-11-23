"""Offline coverage for issue #788: every failed description search reaches the agent.

The 2026-10-10 run ended with 14 literal placeholders and 109 unsourced
stubs. The agentic stage described two thirds of what it was given, but a
stub that cleared the depth floor was selected by no rule, and a thin one
was parked after three attempts the deterministic stage mostly spent.

These tests pin the four rules that close the gap: the agentic stage
selects every unsourced stub every run, a book still at the placeholder
gets one deeper call, the repair policy restarts under version 2, and an
unsourced stub is never reported as a description the editor has.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from adjudication_test_support import AUTHOR
from adjudication_test_support import OPERATION_DATE
from adjudication_test_support import TITLE
from adjudication_test_support import VALID_TEXT
from adjudication_test_support import citation
from adjudication_test_support import fetcher
from adjudication_test_support import page
from adjudication_test_support import response
from adjudication_test_support import workspace
from state_store_support import read_state

from autoscout.content_description import DESCRIPTION_FALLBACK
from autoscout.content_description import apply_description_artifacts
from autoscout.content_description import description_coverage
from autoscout.content_description import prepare_backfill_work_items
from autoscout.description_adjudication import SEARCH_DEPTH_DEEP
from autoscout.description_adjudication import SEARCH_DEPTH_FIELD
from autoscout.description_adjudication import CodexDescriptionAdjudicationAdapter
from autoscout.description_adjudication import ReplayDescriptionAdjudicationAdapter
from autoscout.description_adjudication import adjudicate_descriptions
from autoscout.description_adjudication import build_adjudication_prompt
from autoscout.description_enrichment import _encode_work_items
from autoscout.description_repair import REPAIR_ATTEMPTS_FIELD
from autoscout.description_repair import REPAIR_MAX_ATTEMPTS
from autoscout.description_repair import REPAIR_POLICY_FIELD
from autoscout.description_repair import REPAIR_POLICY_VERSION
from autoscout.description_repair import record_repair_attempt
from autoscout.description_repair import repair_due
from autoscout.description_repair import repair_exhausted
from autoscout.grounded_description import REASON_FEWER_THAN_TWO
from autoscout.grounded_description import canonicalize_url
from autoscout.json_io import read_json
from autoscout.russian_workflow import completion_message

# A guess from the title that clears the depth floor: two sentences, more
# than 200 characters. No rule selected this shape before the issue.
LONG_STUB = (
    "Мифологический атлас связывает древнегреческие предания с реальными ландшафтами, "
    "городами и островами Греции. Его редакционная особенность — возможность читать мифы "
    "одновременно как истории, культурную географию и маршрут по сохранившимся местам памяти."
)
THIN_STUB = "Мемуарная книга о дружбе, любви и трудном личном опыте."
PUBLISHER_URL = "https://publisher-example.test/work-one"
CATALOGUE_URL = "https://catalog-example.test/work-one"


def _record(candidate_id: str, identity_key: str, text: str, provenance: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "identity_key": identity_key,
        "title": TITLE,
        "primary_authors": [AUTHOR],
        "content_description": text,
        "description_provenance": {
            "identity_key": identity_key,
            "content_description": text,
            "provenance": provenance,
            "applied_date": "2026-09-01",
            "evidence_basis": [],
        },
    }


def _two_source(identity_key: str) -> dict[str, Any]:
    return response(
        identity_key,
        [
            citation(PUBLISHER_URL, "publisher-example.test", "publisher"),
            citation(CATALOGUE_URL, "catalog-example.test", "library_catalogue"),
        ],
    )


def _fetcher() -> Any:
    return fetcher({canonicalize_url(PUBLISHER_URL): page(), canonicalize_url(CATALOGUE_URL): page()})


def _exhaust(record: dict[str, Any]) -> None:
    for day in ("2026-08-24", "2026-08-31", "2026-09-07"):
        record_repair_attempt(
            record,
            operation_date=day,
            operation_id=f"op-{day}",
            outcome="attempted_no_description",
            reason_code=REASON_FEWER_THAN_TWO,
            stage="grounded",
        )


# ── Selection: every unsourced stub, every run ────────────────────────


def test_default_selector_still_ignores_a_stub_that_clears_the_depth_floor() -> None:
    history = [_record("candidate-stub", "stub|alex author", LONG_STUB, "codex-generated")]
    assert prepare_backfill_work_items(history, visibility_records=[]) == []
    assert (
        prepare_backfill_work_items(
            history, visibility_records=[], include_depth_repair=True, repair_as_of=OPERATION_DATE
        )
        == []
    )


def test_agentic_selector_takes_a_stub_that_clears_the_depth_floor() -> None:
    history = [
        _record("candidate-stub", "stub|alex author", LONG_STUB, "codex-generated"),
        _record("candidate-sourced", "sourced|alex author", LONG_STUB, "grounded"),
        _record("candidate-editor", "editor|alex author", LONG_STUB, "editor-corrected"),
        _record("candidate-hidden", "hidden|alex author", LONG_STUB, "codex-generated"),
    ]
    items = prepare_backfill_work_items(
        history,
        visibility_records=[{"identity_key": "hidden|alex author", "visibility": "hidden"}],
        include_depth_repair=True,
        include_unsourced_stubs=True,
        repair_as_of=OPERATION_DATE,
    )
    assert [item["identity_key"] for item in items] == ["stub|alex author"]
    assert items[0]["unsourced_stub"] is True
    assert items[0]["replaces_existing_description"] is True
    # It is not thin, so it is not a depth repair and logs no repair attempt.
    assert "depth_repair" not in items[0]


def test_agentic_selector_takes_a_thin_stub_through_cooldown_and_exhaustion() -> None:
    cooling = _record("candidate-cooling", "cooling|alex author", THIN_STUB, "codex-generated")
    record_repair_attempt(
        cooling,
        operation_date=OPERATION_DATE,
        operation_id="op",
        outcome="attempted_no_description",
        reason_code="synthesis_failure",
        stage="grounded",
    )
    exhausted = _record("candidate-exhausted", "exhausted|alex author", THIN_STUB, "codex-generated")
    _exhaust(exhausted)
    assert repair_exhausted(exhausted)
    # A sourced short description stays under the repair policy.
    sourced = _record("candidate-sourced", "sourced|alex author", THIN_STUB, "grounded")
    _exhaust(sourced)

    items = prepare_backfill_work_items(
        [cooling, exhausted, sourced],
        visibility_records=[],
        include_depth_repair=True,
        include_unsourced_stubs=True,
        repair_as_of=OPERATION_DATE,
    )
    by_key = {item["identity_key"]: item for item in items}
    assert set(by_key) == {"cooling|alex author", "exhausted|alex author"}
    for item in by_key.values():
        assert item["unsourced_stub"] is True
        # A thin stub is still a repair, so its attempt reaches the shared log.
        assert item["depth_repair"] is True


# ── Apply gate: a stub has no source to protect ───────────────────────


def test_gate_replaces_a_long_stub_only_with_the_unsourced_permission() -> None:
    history = [_record("candidate-stub", "stub|alex author", LONG_STUB, "codex-generated")]
    artifact = {"identity_key": "stub|alex author", "content_description": VALID_TEXT}

    kept, kept_results = apply_description_artifacts(
        [dict(history[0])],
        [dict(artifact)],
        provenance="agent-adjudicated",
        effective_date=OPERATION_DATE,
        allow_repair_replacement=True,
    )
    assert kept_results["applied"] == 0
    assert kept[0]["content_description"] == LONG_STUB

    replaced, replaced_results = apply_description_artifacts(
        [dict(history[0])],
        [dict(artifact)],
        provenance="agent-adjudicated",
        effective_date=OPERATION_DATE,
        allow_unsourced_replacement=True,
    )
    assert replaced_results["applied"] == 1
    assert replaced[0]["content_description"] == VALID_TEXT
    assert replaced[0]["description_provenance"]["provenance"] == "agent-adjudicated"


def test_gate_unsourced_permission_never_touches_sourced_or_editor_text() -> None:
    sourced = _record("candidate-sourced", "sourced|alex author", LONG_STUB, "grounded")
    editor = _record("candidate-editor", "editor|alex author", LONG_STUB, "editor-corrected")
    updated, results = apply_description_artifacts(
        [sourced, editor],
        [
            {"identity_key": "sourced|alex author", "content_description": VALID_TEXT},
            {"identity_key": "editor|alex author", "content_description": VALID_TEXT},
        ],
        provenance="agent-adjudicated",
        effective_date=OPERATION_DATE,
        allow_unsourced_replacement=True,
    )
    assert results["applied"] == 0
    assert results["skipped"] == 2
    assert [record["content_description"] for record in updated] == [LONG_STUB, LONG_STUB]


def test_gate_rejected_replacement_leaves_the_stub_in_place() -> None:
    history = [_record("candidate-stub", "stub|alex author", LONG_STUB, "codex-generated")]
    updated, results = apply_description_artifacts(
        history,
        [{"identity_key": "stub|alex author", "content_description": "", "rejection_reason": "ambiguity"}],
        provenance="agent-adjudicated",
        effective_date=OPERATION_DATE,
        allow_unsourced_replacement=True,
    )
    assert results["fallback"] == 0
    assert updated[0]["content_description"] == LONG_STUB
    assert updated[0]["description_provenance"]["provenance"] == "codex-generated"
    assert "content_description_rejection_reason" not in updated[0]


def test_gate_replaces_an_autobiographical_stub_that_still_lacks_subject_context() -> None:
    from autoscout.autobiographical_context import autobiographical_subject_context_warning

    stub = _record("candidate-stub", "stub|alex author", LONG_STUB, "codex-generated")
    stub["candidate_category"] = "memoir"
    assert autobiographical_subject_context_warning(stub)

    updated, results = apply_description_artifacts(
        [stub],
        [{"identity_key": "stub|alex author", "content_description": VALID_TEXT}],
        provenance="agent-adjudicated",
        effective_date=OPERATION_DATE,
        allow_unsourced_replacement=True,
    )
    assert results["applied"] == 1
    assert updated[0]["content_description"] == VALID_TEXT
    assert updated[0]["description_provenance"]["provenance"] == "agent-adjudicated"
    # The verified text is in place; the missing context is still reported.
    assert autobiographical_subject_context_warning(updated[0])


def test_gate_still_skips_a_context_only_repair_that_brings_no_context() -> None:
    sourced = _record("candidate-sourced", "sourced|alex author", LONG_STUB, "grounded")
    sourced["candidate_category"] = "memoir"
    updated, results = apply_description_artifacts(
        [sourced],
        [{"identity_key": "sourced|alex author", "content_description": VALID_TEXT}],
        provenance="agent-adjudicated",
        effective_date=OPERATION_DATE,
        allow_unsourced_replacement=True,
    )
    assert results["skipped"] == 1
    assert updated[0]["content_description"] == LONG_STUB


# ── The stage ─────────────────────────────────────────────────────────


def test_stage_replaces_a_long_stub_with_a_verified_description(tmp_path: Path) -> None:
    history = [_record("candidate-stub", "stub|alex author", LONG_STUB, "codex-generated")]
    state_dir, output_dir = workspace(tmp_path, history)

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=ReplayDescriptionAdjudicationAdapter({"stub|alex author": _two_source("stub|alex author")}),
        fetcher=_fetcher(),
        apply=True,
        effective_date=OPERATION_DATE,
        operation_id="issue-788",
    )

    assert result["applied"] == 1
    saved = read_state(state_dir, "candidate_history")[0]
    assert saved["content_description"] == VALID_TEXT
    assert saved["description_provenance"]["provenance"] == "agent-adjudicated"
    # A stub that clears the depth floor is not a depth repair, but its
    # attempt is logged so the worklist order can rotate stubs.
    assert saved[REPAIR_ATTEMPTS_FIELD][-1]["stage"] == "adjudication"
    assert result["repair"]["selected"] == 0


def test_stage_failure_leaves_the_stub_and_is_retried_next_run(tmp_path: Path) -> None:
    history = [_record("candidate-stub", "stub|alex author", LONG_STUB, "codex-generated")]
    state_dir, output_dir = workspace(tmp_path, history)
    adapter = ReplayDescriptionAdjudicationAdapter()

    for _run in range(2):
        result = adjudicate_descriptions(
            state_dir,
            output_dir,
            adapter=adapter,
            fetcher=_fetcher(),
            apply=True,
            effective_date=OPERATION_DATE,
            operation_id="issue-788",
            deep_pass=True,
        )
        assert result["applied"] == 0

    saved = read_state(state_dir, "candidate_history")[0]
    assert saved["content_description"] == LONG_STUB
    assert saved["description_provenance"] == history[0]["description_provenance"]
    assert "content_description_rejection_reason" not in saved
    # Once per run, with no cooldown in between. The deeper call is for
    # books with no text at all, so a stub is never searched twice in a run.
    assert adapter.calls == ["stub|alex author", "stub|alex author"]


def test_stage_takes_an_exhausted_thin_stub_and_logs_the_attempt(tmp_path: Path) -> None:
    exhausted = _record("candidate-stub", "stub|alex author", THIN_STUB, "codex-generated")
    _exhaust(exhausted)
    state_dir, output_dir = workspace(tmp_path, [exhausted])

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=ReplayDescriptionAdjudicationAdapter({"stub|alex author": _two_source("stub|alex author")}),
        fetcher=_fetcher(),
        apply=True,
        effective_date=OPERATION_DATE,
        operation_id="issue-788",
    )

    assert result["applied"] == 1
    saved = read_state(state_dir, "candidate_history")[0]
    assert saved["content_description"] == VALID_TEXT
    assert saved[REPAIR_ATTEMPTS_FIELD][-1]["stage"] == "adjudication"
    assert saved[REPAIR_ATTEMPTS_FIELD][-1]["outcome"] == "applied"
    assert result["repair"]["deferred_exhausted"] == 0


def test_limit_admits_missing_text_then_stubs_then_sourced_repairs(tmp_path: Path) -> None:
    history = [
        _record("candidate-a-sourced", "sourced|alex author", THIN_STUB, "grounded"),
        _record("candidate-b-stub", "stub|alex author", LONG_STUB, "codex-generated"),
        _record("candidate-c-missing", "missing|alex author", DESCRIPTION_FALLBACK, "fallback"),
    ]
    state_dir, output_dir = workspace(tmp_path, history)
    adapter = ReplayDescriptionAdjudicationAdapter()

    adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=adapter,
        fetcher=_fetcher(),
        effective_date=OPERATION_DATE,
        limit=2,
        max_workers=1,
    )

    assert adapter.calls == ["missing|alex author", "stub|alex author"]


def test_a_stub_that_keeps_failing_does_not_hold_its_slot_under_the_cap(tmp_path: Path) -> None:
    history = [
        _record("candidate-a", "stub a|alex author", LONG_STUB, "codex-generated"),
        _record("candidate-b", "stub b|alex author", LONG_STUB, "codex-generated"),
        _record("candidate-c", "stub c|alex author", THIN_STUB, "codex-generated"),
    ]
    state_dir, output_dir = workspace(tmp_path, history)
    adapter = ReplayDescriptionAdjudicationAdapter()

    for day in ("2026-09-14", "2026-09-21", "2026-09-28"):
        adjudicate_descriptions(
            state_dir,
            output_dir,
            adapter=adapter,
            fetcher=_fetcher(),
            apply=True,
            effective_date=day,
            operation_id=f"issue-788-{day}",
            limit=1,
        )

    # One slot a week and every stub fails: each one still gets its turn.
    assert adapter.calls == ["stub a|alex author", "stub b|alex author", "stub c|alex author"]


# ── The deeper second call ────────────────────────────────────────────


class _DeepOnlyAdapter:
    """Decline the first call and answer the deeper one."""

    def __init__(self, deep_responses: dict[str, dict[str, Any]] | None = None) -> None:
        self._deep_responses = deep_responses or {}
        self.seen: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def adjudicate(self, work_item: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.seen.append(dict(work_item))
        identity_key = str(work_item.get("identity_key", ""))
        if work_item.get(SEARCH_DEPTH_FIELD) == SEARCH_DEPTH_DEEP and identity_key in self._deep_responses:
            return dict(self._deep_responses[identity_key])
        return response(identity_key, [], text="", resolved=False, unresolved_reason="nothing found")


def _placeholder_history() -> list[dict[str, Any]]:
    return [_record("candidate-missing", "missing|alex author", DESCRIPTION_FALLBACK, "fallback")]


def test_deep_pass_describes_a_book_the_first_call_left_at_the_placeholder(tmp_path: Path) -> None:
    state_dir, output_dir = workspace(tmp_path, _placeholder_history())
    adapter = _DeepOnlyAdapter({"missing|alex author": _two_source("missing|alex author")})

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=adapter,  # type: ignore[arg-type]
        fetcher=_fetcher(),
        apply=True,
        effective_date=OPERATION_DATE,
        operation_id="issue-788",
        deep_pass=True,
    )

    assert [item.get(SEARCH_DEPTH_FIELD) for item in adapter.seen] == [None, SEARCH_DEPTH_DEEP]
    assert result["applied"] == 1
    saved = read_state(state_dir, "candidate_history")[0]
    assert saved["content_description"] == VALID_TEXT
    report = result["description_adjudication_stage_report"]
    assert report["deep_pass"] == {"attempted": 1, "applied": 1, "failed": 0}
    assert report["counts"]["still_fallback"] == 0
    row = read_json(output_dir / "description-adjudication-preview.json")["preview"][0]
    assert row["deep_pass"]["first_decision"] == "fallback"
    assert row["deep_pass"]["decision"] == "apply_two_source"


def test_deep_pass_that_also_declines_keeps_the_first_verdict(tmp_path: Path) -> None:
    state_dir, output_dir = workspace(tmp_path, _placeholder_history())
    adapter = _DeepOnlyAdapter()

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=adapter,  # type: ignore[arg-type]
        fetcher=_fetcher(),
        apply=True,
        effective_date=OPERATION_DATE,
        operation_id="issue-788",
        deep_pass=True,
    )

    assert len(adapter.seen) == 2
    assert result["applied"] == 0
    assert read_state(state_dir, "candidate_history")[0]["content_description"] == DESCRIPTION_FALLBACK
    report = result["description_adjudication_stage_report"]
    assert report["deep_pass"] == {"attempted": 1, "applied": 0, "failed": 0}
    assert report["attention_required"] is False
    assert report["counts"]["still_fallback"] == 1


class _FailingCallAdapter:
    """Decline one search depth and fail the other like a timed-out call."""

    def __init__(self, failing_depth: str | None) -> None:
        self._failing_depth = failing_depth

    def adjudicate(self, work_item: dict[str, Any]) -> dict[str, Any]:
        if work_item.get(SEARCH_DEPTH_FIELD) == self._failing_depth:
            raise TimeoutError("call ran long")
        identity_key = str(work_item.get("identity_key", ""))
        return response(identity_key, [], text="", resolved=False, unresolved_reason="nothing found")


def _run_with_failing_call(tmp_path: Path, failing_depth: str | None) -> dict[str, Any]:
    state_dir, output_dir = workspace(tmp_path, _placeholder_history())
    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=_FailingCallAdapter(failing_depth),  # type: ignore[arg-type]
        fetcher=_fetcher(),
        apply=True,
        effective_date=OPERATION_DATE,
        operation_id="issue-788",
        deep_pass=True,
    )
    row = read_json(output_dir / "description-adjudication-preview.json")["preview"][0]
    return {"report": result["description_adjudication_stage_report"], "row": row}


def test_a_failed_deeper_call_behind_a_decline_still_raises_attention(tmp_path: Path) -> None:
    outcome = _run_with_failing_call(tmp_path, SEARCH_DEPTH_DEEP)
    report, row = outcome["report"], outcome["row"]
    # The row keeps the first call's decline, so the row alone shows no failure.
    assert row["decision"] == "fallback"
    assert report["counts"]["failed"] == 0
    assert report["deep_pass"] == {"attempted": 1, "applied": 0, "failed": 1}
    assert report["attention_required"] is True
    assert row["deep_pass"]["decision"] == "invalid"
    assert "TimeoutError" in row["deep_pass"]["decision_reason"]


def test_a_failed_first_call_behind_a_deeper_decline_still_raises_attention(tmp_path: Path) -> None:
    outcome = _run_with_failing_call(tmp_path, None)
    report, row = outcome["report"], outcome["row"]
    assert row["decision"] == "fallback"
    assert report["deep_pass"] == {"attempted": 1, "applied": 0, "failed": 1}
    assert report["attention_required"] is True
    assert row["deep_pass"]["first_decision"] == "invalid"
    assert "TimeoutError" in row["deep_pass"]["first_decision_reason"]


def test_a_cached_first_decline_still_gets_its_deeper_search_once(tmp_path: Path) -> None:
    from autoscout.description_adjudication import DEEP_SEARCH_DATE_FIELD

    # The first search found one owner: a decline that retains its evidence,
    # which the next run would reuse without calling the agent at all.
    one_owner = response("missing|alex author", [citation(PUBLISHER_URL, "publisher-example.test", "publisher")])
    thin = dict(one_owner, content_description=THIN_STUB)
    state_dir, output_dir = workspace(tmp_path, _placeholder_history())
    shallow = ReplayDescriptionAdjudicationAdapter({"missing|alex author": thin})
    adjudicate_descriptions(
        state_dir, output_dir, adapter=shallow, fetcher=_fetcher(), apply=True, effective_date=OPERATION_DATE
    )
    cached = read_state(state_dir, "candidate_history")[0]
    assert cached["content_description"] == DESCRIPTION_FALLBACK
    assert cached["description_provenance"]["evidence_digests"]
    assert DEEP_SEARCH_DATE_FIELD not in cached

    def _run(adapter: Any) -> dict[str, Any]:
        return adjudicate_descriptions(
            state_dir,
            output_dir,
            adapter=adapter,
            fetcher=_fetcher(),
            apply=True,
            effective_date=OPERATION_DATE,
            deep_pass=True,
        )

    declining = _DeepOnlyAdapter()
    first = _run(declining)
    # Only the deeper call runs: the first search's verdict is the cached one.
    assert [item.get(SEARCH_DEPTH_FIELD) for item in declining.seen] == [SEARCH_DEPTH_DEEP]
    assert first["evidence_unchanged"] == 0
    saved = read_state(state_dir, "candidate_history")[0]
    assert saved[DEEP_SEARCH_DATE_FIELD] == OPERATION_DATE
    assert saved["description_provenance"] == cached["description_provenance"]

    again = _DeepOnlyAdapter()
    second = _run(again)
    # The deeper search has run, so evidence reuse resumes.
    assert again.seen == []
    assert second["evidence_unchanged"] == 1


def _cached_decline_workspace(tmp_path: Path) -> tuple[Path, Path]:
    one_owner = response("missing|alex author", [citation(PUBLISHER_URL, "publisher-example.test", "publisher")])
    state_dir, output_dir = workspace(tmp_path, _placeholder_history())
    adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=ReplayDescriptionAdjudicationAdapter(
            {"missing|alex author": dict(one_owner, content_description=THIN_STUB)}
        ),
        fetcher=_fetcher(),
        apply=True,
        effective_date=OPERATION_DATE,
    )
    return state_dir, output_dir


def test_a_failed_deeper_search_of_a_cached_decline_is_retried_next_run(tmp_path: Path) -> None:
    from autoscout.description_adjudication import DEEP_SEARCH_DATE_FIELD

    state_dir, output_dir = _cached_decline_workspace(tmp_path)

    class _CountingFailure(_FailingCallAdapter):
        calls = 0

        def adjudicate(self, work_item: dict[str, Any]) -> dict[str, Any]:
            type(self).calls += 1
            return super().adjudicate(work_item)

    for _run in range(2):
        adjudicate_descriptions(
            state_dir,
            output_dir,
            adapter=_CountingFailure(SEARCH_DEPTH_DEEP),  # type: ignore[arg-type]
            fetcher=_fetcher(),
            apply=True,
            effective_date=OPERATION_DATE,
            deep_pass=True,
        )

    assert _CountingFailure.calls == 2
    assert DEEP_SEARCH_DATE_FIELD not in read_state(state_dir, "candidate_history")[0]


def test_a_cached_decline_waits_when_the_stage_clock_cannot_fit_the_deeper_call(tmp_path: Path) -> None:
    state_dir, output_dir = _cached_decline_workspace(tmp_path)
    adapter = _DeepOnlyAdapter()
    # Stage start, the admission check, then 100 seconds spent re-fetching
    # the retained page before the deeper call's own check.
    ticks = iter([0.0, 0.0, 100.0])

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=adapter,  # type: ignore[arg-type]
        fetcher=_fetcher(),
        apply=True,
        effective_date=OPERATION_DATE,
        deep_pass=True,
        stage_budget_seconds=300.0,
        max_workers=1,
        monotonic=lambda: next(ticks),
    )

    assert adapter.seen == []
    assert result["evidence_unchanged"] == 1


def test_a_failed_deeper_retry_clears_an_earlier_deep_search_date(tmp_path: Path) -> None:
    from autoscout.description_adjudication import DEEP_SEARCH_DATE_FIELD

    history = _placeholder_history()
    history[0][DEEP_SEARCH_DATE_FIELD] = "2026-08-01"
    state_dir, output_dir = workspace(tmp_path, history)

    adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=_FailingCallAdapter(SEARCH_DEPTH_DEEP),  # type: ignore[arg-type]
        fetcher=_fetcher(),
        apply=True,
        effective_date=OPERATION_DATE,
        deep_pass=True,
    )

    assert DEEP_SEARCH_DATE_FIELD not in read_state(state_dir, "candidate_history")[0]


def test_a_first_call_without_its_deeper_call_clears_an_earlier_deep_search_date(tmp_path: Path) -> None:
    from autoscout.description_adjudication import DEEP_SEARCH_DATE_FIELD

    history = _placeholder_history()
    history[0][DEEP_SEARCH_DATE_FIELD] = "2026-08-01"
    state_dir, output_dir = workspace(tmp_path, history)
    one_owner = response("missing|alex author", [citation(PUBLISHER_URL, "publisher-example.test", "publisher")])
    shallow = ReplayDescriptionAdjudicationAdapter(
        {"missing|alex author": dict(one_owner, content_description=THIN_STUB)}
    )
    # The first call runs and retains fresh evidence; 100 seconds later the
    # stage clock no longer fits the deeper call.
    ticks = iter([0.0, 0.0, 100.0])
    adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=shallow,
        fetcher=_fetcher(),
        apply=True,
        effective_date=OPERATION_DATE,
        deep_pass=True,
        stage_budget_seconds=300.0,
        max_workers=1,
        monotonic=lambda: next(ticks),
    )
    saved = read_state(state_dir, "candidate_history")[0]
    assert saved["description_provenance"]["evidence_digests"]
    assert DEEP_SEARCH_DATE_FIELD not in saved

    # The next run owes the deeper search, and the unchanged evidence does
    # not stand in its way.
    deeper = _DeepOnlyAdapter()
    adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=deeper,  # type: ignore[arg-type]
        fetcher=_fetcher(),
        apply=True,
        effective_date=OPERATION_DATE,
        deep_pass=True,
    )
    assert [item.get(SEARCH_DEPTH_FIELD) for item in deeper.seen] == [SEARCH_DEPTH_DEEP]


def test_completion_message_names_a_failed_call_on_a_retained_stub_not_a_source(tmp_path: Path) -> None:
    history = [_record("candidate-stub", "stub|alex author", LONG_STUB, "codex-generated")]
    state_dir, output_dir = workspace(tmp_path, history)
    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=_FailingCallAdapter(None),  # type: ignore[arg-type]
        fetcher=_fetcher(),
        apply=True,
        effective_date=OPERATION_DATE,
        deep_pass=True,
    )
    report = result["description_adjudication_stage_report"]
    assert report["counts"]["failed"] == 1
    assert report["records"][0]["final_state"] == "description_present"

    message = completion_message(
        "completed/attention-required",
        attention_required=True,
        attention_reasons=("descriptions",),
        adjudication_stage_report=report,
    )
    assert "Часть обращений к агенту при проверке описаний завершилась сбоем." in message
    assert "Часть источников не была проверена" not in message


def test_completion_message_names_a_recovered_agent_failure_not_a_source() -> None:
    report = {
        "counts": {"applied": 1, "skipped": 0, "failed": 0, "ambiguous": 0, "still_fallback": 0},
        "records": [{"candidate_id": "candidate-missing", "result": "applied", "final_state": "description_present"}],
        "deep_pass": {"attempted": 1, "applied": 1, "failed": 1},
    }
    message = completion_message(
        "completed/attention-required",
        attention_required=True,
        attention_reasons=("descriptions",),
        adjudication_stage_report=report,
    )
    assert "Часть обращений к агенту при проверке описаний завершилась сбоем." in message
    assert "Часть источников не была проверена" not in message


def test_without_deep_pass_a_placeholder_book_gets_one_call(tmp_path: Path) -> None:
    state_dir, output_dir = workspace(tmp_path, _placeholder_history())
    adapter = _DeepOnlyAdapter({"missing|alex author": _two_source("missing|alex author")})

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=adapter,  # type: ignore[arg-type]
        fetcher=_fetcher(),
        apply=True,
        effective_date=OPERATION_DATE,
        operation_id="issue-788",
    )

    assert len(adapter.seen) == 1
    assert result["applied"] == 0
    assert "deep_pass" not in result["description_adjudication_stage_report"]


def test_deep_pass_is_not_started_when_the_stage_clock_cannot_fit_it(tmp_path: Path) -> None:
    state_dir, output_dir = workspace(tmp_path, _placeholder_history())
    adapter = _DeepOnlyAdapter({"missing|alex author": _two_source("missing|alex author")})
    # Stage start, the first call's admission check, then the deeper call's
    # admission check 100 seconds later: 240 more seconds no longer fit.
    ticks = iter([0.0, 0.0, 100.0])

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=adapter,  # type: ignore[arg-type]
        fetcher=_fetcher(),
        apply=True,
        effective_date=OPERATION_DATE,
        operation_id="issue-788",
        deep_pass=True,
        stage_budget_seconds=300.0,
        max_workers=1,
        monotonic=lambda: next(ticks),
    )

    assert len(adapter.seen) == 1
    assert result["applied"] == 0
    assert "deep_pass" not in result["description_adjudication_stage_report"]


def test_weekly_production_stage_enables_the_deep_pass(monkeypatch: Any) -> None:
    from autoscout import description_adjudication
    from autoscout import weekly_workflow

    monkeypatch.setattr(description_adjudication, "_pinned_model_and_effort", lambda: ("", ""))
    service = weekly_workflow._make_production_adjudication()
    assert service._deep_pass is True  # type: ignore[attr-defined]
    assert weekly_workflow._make_deterministic_adjudication()._deep_pass is False  # type: ignore[attr-defined]


def test_deep_prompt_adds_search_guidance_and_keeps_every_rule() -> None:
    default = build_adjudication_prompt()
    deep = build_adjudication_prompt(deep=True)
    assert "ISBN" not in default
    assert "ISBN" in deep
    assert "original language" in deep
    assert "publisher's own page" in deep
    rules = default[default.index("Rules:") :]
    assert deep.endswith(rules)
    assert deep.startswith(default[: default.index("Rules:")])


def test_codex_adapter_sends_the_deep_prompt_only_for_a_deep_work_item() -> None:
    prompts: list[str] = []

    def _runner(command: list[str], **_kwargs: Any) -> Any:
        prompts.append(command[-1])
        raise OSError("no cli in tests")

    adapter = CodexDescriptionAdjudicationAdapter(runner=_runner, model="", reasoning_effort="")
    item = {"identity_key": "missing|alex author", "title": TITLE, "metadata": {"title": TITLE}}
    for work_item in (item, {**item, SEARCH_DEPTH_FIELD: SEARCH_DEPTH_DEEP}):
        try:
            adapter.adjudicate(work_item)
        except Exception:
            pass
    assert prompts == [build_adjudication_prompt(), build_adjudication_prompt(deep=True)]


def test_isbn_reaches_the_agent_as_a_search_key() -> None:
    record = _record("candidate-missing", "missing|alex author", DESCRIPTION_FALLBACK, "fallback")
    record["isbn"] = "9789045051932"
    items = prepare_backfill_work_items([record], visibility_records=[])
    assert items[0]["metadata"]["isbn"] == "9789045051932"
    assert _encode_work_items(items)[0]["metadata"]["isbn"] == "9789045051932"


# ── Repair policy version 2 ───────────────────────────────────────────


def test_policy_version_two_restarts_a_record_parked_under_version_one() -> None:
    assert REPAIR_POLICY_VERSION == 2
    parked = _record("candidate-sourced", "sourced|alex author", THIN_STUB, "grounded")
    parked[REPAIR_POLICY_FIELD] = 1
    parked[REPAIR_ATTEMPTS_FIELD] = [
        {
            "date": day,
            "operation_id": f"op-{day}",
            "outcome": "attempted_no_description",
            "reason_code": REASON_FEWER_THAN_TWO,
            "stage": stage,
        }
        for day, stage in (("2026-09-19", "grounded"), ("2026-09-26", "adjudication"), ("2026-10-03", "grounded"))
    ]
    assert len(parked[REPAIR_ATTEMPTS_FIELD]) == REPAIR_MAX_ATTEMPTS
    from datetime import date

    assert not repair_exhausted(parked)
    assert repair_due(parked, date(2026, 10, 10))


# ── Coverage: a stub is not a description the editor has ──────────────


def _coverage_candidates() -> list[dict[str, Any]]:
    return [
        _record("candidate-sourced", "sourced|alex author", VALID_TEXT, "grounded"),
        _record("candidate-adjudicated", "adjudicated|alex author", VALID_TEXT, "agent-adjudicated"),
        _record("candidate-long-stub", "long stub|alex author", LONG_STUB, "codex-generated"),
        _record("candidate-thin-stub", "thin stub|alex author", THIN_STUB, "codex-generated"),
        _record("candidate-missing", "missing|alex author", DESCRIPTION_FALLBACK, "fallback"),
    ]


def test_coverage_counts_unsourced_stubs_apart_from_sourced_descriptions() -> None:
    coverage = description_coverage(_coverage_candidates())
    assert coverage["total_candidates"] == 5
    # Safety validity keeps its meaning: a stub is safe Russian prose.
    assert coverage["validated_count"] == 4
    assert coverage["unsourced_count"] == 2
    assert coverage["sourced_count"] == 2
    assert coverage["sourced_count"] + coverage["unsourced_count"] == coverage["validated_count"]
    assert coverage["fallback_count"] == 1
    # The thin stub is below the floor, but it is not a verified description.
    assert coverage["below_depth_count"] == 1
    assert coverage["sourced_below_depth_count"] == 0


def test_review_header_depth_clause_counts_only_verified_descriptions() -> None:
    from autoscout.html_review import build_html_review
    from autoscout.review_rendering import assemble_cumulative_review

    history = [
        _record("candidate-thin-stub", "thin stub|alex author", THIN_STUB, "codex-generated"),
        _record("candidate-thin-sourced", "thin sourced|alex author", THIN_STUB, "grounded"),
    ]
    for record in history:
        record.update({"first_seen_date": "2026-09-01", "last_seen_date": OPERATION_DATE})
    rows, summary = assemble_cumulative_review([], history, [])
    html = build_html_review(rows, {"cumulative_counts": summary})
    assert "проверено — 1 из 2, не подтверждены источниками — 1, требуют дополнения — 0." in html
    assert "Из проверенных краткими остаются — 1 " in html


def test_review_coverage_counts_the_rows_the_editor_sees_marked_unsourced() -> None:
    from autoscout.content_description import DESCRIPTION_UNCONFIRMED_STATUS
    from autoscout.review_rendering import assemble_cumulative_review

    history = _coverage_candidates()
    for record in history:
        record.update({"first_seen_date": "2026-09-01", "last_seen_date": OPERATION_DATE})
    rows, summary = assemble_cumulative_review([], history, [])
    marked = [row for row in rows if row["description_status"] == DESCRIPTION_UNCONFIRMED_STATUS]
    coverage = summary["description_coverage"]
    assert len(marked) == 2
    assert coverage["unsourced_count"] == 2
    assert coverage["sourced_count"] == 2
    assert coverage["fallback_count"] == 1


def test_completion_message_does_not_count_stubs_as_descriptions() -> None:
    message = completion_message(
        "completed/no-attention",
        description_coverage=description_coverage(_coverage_candidates()),
    )
    assert "Описаний на русском: 2 из 5." in message
    assert "Не подтверждены источниками: 2." in message
    assert "Требуют проверки: 1." in message


def test_completion_message_reads_coverage_written_before_the_split_unchanged() -> None:
    message = completion_message(
        "completed/no-attention",
        description_coverage={"validated_count": 4, "fallback_count": 1, "total_candidates": 5},
    )
    assert "Описаний на русском: 4 из 5." in message
    assert "Не подтверждены источниками" not in message
