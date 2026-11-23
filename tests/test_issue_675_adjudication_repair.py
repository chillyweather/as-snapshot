"""Offline coverage for issue #675: adjudication as the residual repair route.

The adjudication stage selects retained thin descriptions under the
shared repair policy, applies qualifying replacements through a scoped
repair permission (never a general valid-description overwrite), skips
the evidence-reuse shortcut for due repairs, and logs every attempt into
the shared repair log so cross-stage retries obey one cooldown and
exhaustion policy. Failures preserve retained text and provenance, and
no unrelated editorial state is modified.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from adjudication_test_support import citation as _citation
from adjudication_test_support import fetcher as _fetcher_for_pages
from adjudication_test_support import page as _page
from adjudication_test_support import response as _response
from state_store_support import read_state
from state_store_support import seed_state

from autoscout.content_description import DESCRIPTION_FALLBACK
from autoscout.content_description import apply_description_artifacts
from autoscout.content_description import prepare_backfill_work_items
from autoscout.description_adjudication import ReplayDescriptionAdjudicationAdapter
from autoscout.description_adjudication import adjudicate_descriptions
from autoscout.description_repair import REPAIR_ATTEMPTS_FIELD
from autoscout.grounded_description import REASON_FEWER_THAN_TWO
from autoscout.grounded_description import REASON_INSUFFICIENT_DEPTH
from autoscout.grounded_description import canonicalize_url
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
PUBLISHER_URL = "https://publisher.example.test/test-book"
CATALOGUE_URL = "https://catalog.example.test/test-book"


def _record(candidate_id: str, identity_key: str, text: str, provenance: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "identity_key": identity_key,
        "title": "Test Book",
        "primary_authors": ["Test Author"],
        "content_description": text,
        "description_provenance": {
            "identity_key": identity_key,
            "content_description": text,
            "provenance": provenance,
            "applied_date": "2026-09-01",
            "evidence_basis": [PUBLISHER_URL],
        },
    }


def _thin_records() -> list[dict[str, Any]]:
    return [
        _record("candidate-grounded", "grounded work|test author", THIN_TEXT, "grounded"),
        _record("candidate-codex", "codex work|test author", THIN_TEXT, "codex-generated"),
        _record("candidate-backfilled", "backfilled work|test author", THIN_TEXT, "backfilled"),
        _record("candidate-rich", "rich work|test author", RICH_TEXT, "grounded"),
        _record("candidate-editor", "editor work|test author", THIN_TEXT, "editor-corrected"),
        _record("candidate-fallback", "fallback work|test author", DESCRIPTION_FALLBACK, "fallback"),
    ]


# ── Selection ─────────────────────────────────────────────────────────


def test_selector_ignores_thin_records_by_default() -> None:
    items = prepare_backfill_work_items(_thin_records(), visibility_records=[])
    keys = {item["identity_key"] for item in items}
    assert "fallback work|test author" in keys
    assert "grounded work|test author" not in keys
    assert "codex work|test author" not in keys
    assert "backfilled work|test author" not in keys
    assert "rich work|test author" not in keys
    assert "editor work|test author" not in keys


def test_selector_admits_due_repairs_with_marker() -> None:
    items = prepare_backfill_work_items(
        _thin_records(), visibility_records=[], include_depth_repair=True, repair_as_of=OPERATION_DATE
    )
    by_key = {item["identity_key"]: item for item in items}
    for key in ("grounded work|test author", "codex work|test author", "backfilled work|test author"):
        assert by_key[key]["depth_repair"] is True
        assert by_key[key]["replaces_existing_description"] is True
    assert "rich work|test author" not in by_key
    assert "editor work|test author" not in by_key


def test_selector_respects_hidden_cooldown_and_exhaustion() -> None:
    from autoscout.description_repair import record_repair_attempt

    records = _thin_records()
    cooling = next(item for item in records if item["candidate_id"] == "candidate-grounded")
    record_repair_attempt(
        cooling,
        operation_date=OPERATION_DATE,
        operation_id="op",
        outcome="attempted_no_description",
        reason_code=REASON_FEWER_THAN_TWO,
    )
    exhausted = next(item for item in records if item["candidate_id"] == "candidate-codex")
    for day in ("2026-09-01", "2026-09-08", "2026-09-15"):
        record_repair_attempt(
            exhausted,
            operation_date=day,
            operation_id=f"op-{day}",
            outcome="attempted_no_description",
            reason_code=REASON_FEWER_THAN_TWO,
        )
    visibility = [{"identity_key": "backfilled work|test author", "visibility": "hidden"}]
    items = prepare_backfill_work_items(
        records, visibility_records=visibility, include_depth_repair=True, repair_as_of=OPERATION_DATE
    )
    assert {item["identity_key"] for item in items} == {"fallback work|test author"}


# ── Shared apply gate ─────────────────────────────────────────────────


def test_gate_replacement_needs_explicit_repair_permission() -> None:
    history = [_record("candidate-001", "work one|test author", THIN_TEXT, "grounded")]
    artifact = [{"identity_key": "work one|test author", "content_description": RICH_TEXT}]

    default_updated, default_results = apply_description_artifacts(
        [dict(history[0])], [dict(artifact[0])], provenance="agent-adjudicated", effective_date=OPERATION_DATE
    )
    assert default_results["applied"] == 0
    assert default_results["skipped"] == 1
    assert default_updated[0]["content_description"] == THIN_TEXT

    repaired_updated, repaired_results = apply_description_artifacts(
        [dict(history[0])],
        [dict(artifact[0])],
        provenance="agent-adjudicated",
        effective_date=OPERATION_DATE,
        allow_repair_replacement=True,
    )
    assert repaired_results["applied"] == 1
    assert repaired_updated[0]["content_description"] == RICH_TEXT
    assert repaired_updated[0]["description_provenance"]["provenance"] == "agent-adjudicated"


def test_gate_repair_permission_ignores_rich_and_editor_records() -> None:
    rich = _record("candidate-rich", "rich work|test author", RICH_TEXT, "grounded")
    editor = _record("candidate-editor", "editor work|test author", THIN_TEXT, "editor-corrected")
    updated, results = apply_description_artifacts(
        [rich, editor],
        [
            {"identity_key": "rich work|test author", "content_description": RICH_TEXT + " Иначе."},
            {"identity_key": "editor work|test author", "content_description": RICH_TEXT},
        ],
        provenance="agent-adjudicated",
        effective_date=OPERATION_DATE,
        allow_repair_replacement=True,
    )
    assert results["applied"] == 0
    assert results["skipped"] == 2
    assert updated[0]["content_description"] == RICH_TEXT
    assert updated[1]["content_description"] == THIN_TEXT


# ── Offline adjudication harness ──────────────────────────────────────


def _canned(text_by_key: dict[str, str]) -> dict[str, dict[str, Any]]:
    canned = {}
    for identity_key, text in text_by_key.items():
        canned[identity_key] = _response(
            identity_key,
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
            text=text,
        )
    return canned


def _fetcher() -> Any:
    subject = "A novel about memory, society and public life."
    return _fetcher_for_pages(
        {
            canonicalize_url(PUBLISHER_URL): _page(title="Test Book", author="Test Author", subject=subject),
            canonicalize_url(CATALOGUE_URL): _page(title="Test Book", author="Test Author", subject=subject),
        }
    )


def _workspace(tmp_path: Any, history: list[dict[str, Any]]) -> tuple[Any, Any]:
    tmp_path = Path(tmp_path)
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "run"
    state_dir.mkdir()
    output_dir.mkdir()
    seed_state(state_dir, candidate_history=history)
    seed_state(state_dir, candidate_visibility=[])
    write_json(
        output_dir / "scouting-candidates.json",
        [
            {
                "candidate_id": record["candidate_id"],
                "identity_key": record["identity_key"],
                "title": "Test Book",
                "primary_authors": ["Test Author"],
            }
            for record in history
        ],
    )
    return state_dir, output_dir


def test_repair_replacement_applies_over_retained_thin_text(tmp_path: Any) -> None:
    history = _thin_records()
    state_dir, output_dir = _workspace(tmp_path, history)

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=ReplayDescriptionAdjudicationAdapter(
            _canned(
                {
                    "grounded work|test author": RICH_TEXT,
                    "codex work|test author": RICH_TEXT,
                    "backfilled work|test author": RICH_TEXT,
                }
            )
        ),
        fetcher=_fetcher(),
        apply=True,
        effective_date=OPERATION_DATE,
        operation_id="issue-675",
    )

    assert result["status"] == "ok"
    assert result["applied"] == 3
    saved = {record["candidate_id"]: record for record in read_state(state_dir, "candidate_history")}
    for candidate_id in ("candidate-grounded", "candidate-codex", "candidate-backfilled"):
        assert saved[candidate_id]["content_description"] == RICH_TEXT
        assert saved[candidate_id]["description_provenance"]["provenance"] == "agent-adjudicated"
        assert REPAIR_ATTEMPTS_FIELD in saved[candidate_id]
    assert saved["candidate-rich"]["content_description"] == RICH_TEXT
    assert REPAIR_ATTEMPTS_FIELD not in saved["candidate-rich"]
    assert saved["candidate-editor"]["content_description"] == THIN_TEXT
    assert REPAIR_ATTEMPTS_FIELD not in saved["candidate-editor"]
    repair = result["repair"]
    assert repair["backlog"] == 3
    assert repair["selected"] == 3
    assert repair["attempted"] == 3
    assert repair["improved"] == 3
    assert repair["deferred_budget"] == 0
    assert result["description_projection"]["current"]["projected"] >= 3


def test_shallow_replacement_is_rejected_and_text_preserved(tmp_path: Any) -> None:
    history = [_record("candidate-001", "work one|test author", THIN_TEXT, "grounded")]
    state_dir, output_dir = _workspace(tmp_path, history)

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=ReplayDescriptionAdjudicationAdapter(_canned({"work one|test author": THIN_TEXT})),
        fetcher=_fetcher(),
        apply=True,
        effective_date=OPERATION_DATE,
        operation_id="issue-675",
    )

    assert result["status"] == "ok"
    assert result["applied"] == 0
    saved = read_state(state_dir, "candidate_history")
    assert saved[0]["content_description"] == THIN_TEXT
    assert saved[0]["description_provenance"]["provenance"] == "grounded"
    # The retained thin text is preserved as-is: no fallback overwrites it
    # and no rejection reason is attached to content that still stands.
    assert "content_description_rejection_reason" not in saved[0]
    # The failed repair still counts toward the shared exhaustion limit.
    assert saved[0][REPAIR_ATTEMPTS_FIELD][-1]["reason_code"] == REASON_INSUFFICIENT_DEPTH
    assert result["repair"]["unresolved_insufficient_evidence"] == 1


def test_repair_bypasses_evidence_unchanged_shortcut(tmp_path: Any) -> None:
    from autoscout.description_adjudication import adjudication_evidence_digest

    subject = "A novel about memory, society and public life."
    digest = adjudication_evidence_digest(subject)
    record = _record("candidate-001", "work one|test author", THIN_TEXT, "agent-adjudicated")
    record["description_provenance"]["evidence_basis"] = [PUBLISHER_URL, CATALOGUE_URL]
    record["description_provenance"]["evidence_digests"] = [digest, digest]
    record["grounded_outcome"] = "grounded"
    # Thin agent-adjudicated text is not a repair provenance, so craft the
    # repair-eligible shape the gate actually compares: a thin grounded
    # record whose retained digests match the pages below.
    record["description_provenance"]["provenance"] = "grounded"
    record["description_provenance"]["content_description"] = THIN_TEXT
    state_dir, output_dir = _workspace(tmp_path, [record])

    called: list[str] = []

    class _RecordingAdapter:
        def adjudicate(self, work_item: Any) -> Any:
            called.append(str(work_item.get("identity_key", "")))
            return _response(
                "work one|test author",
                [
                    _citation(
                        PUBLISHER_URL,
                        "publisher.example.test",
                        "publisher",
                        title="Test Book",
                        authors=["Test Author"],
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

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=_RecordingAdapter(),  # type: ignore[arg-type]
        fetcher=_fetcher(),
        apply=True,
        effective_date=OPERATION_DATE,
        operation_id="issue-675",
    )

    assert result["status"] == "ok"
    assert called == ["work one|test author"]
    saved = read_state(state_dir, "candidate_history")
    assert saved[0]["content_description"] == RICH_TEXT


def test_cross_stage_cooldown_defers_automatic_retry(tmp_path: Path) -> None:
    from autoscout.description_repair import record_repair_attempt

    record = _record("candidate-001", "work one|test author", THIN_TEXT, "grounded")
    # Grounded repair failed yesterday: the residual stage must not retry today.
    record_repair_attempt(
        record,
        operation_date="2026-09-13",
        operation_id="grounded-op",
        outcome="attempted_no_description",
        reason_code=REASON_FEWER_THAN_TWO,
    )
    state_dir, output_dir = _workspace(tmp_path, [record])

    class _Boom:
        def adjudicate(self, work_item: Any) -> Any:
            raise AssertionError("cooling records must not reach the adapter")

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=_Boom(),  # type: ignore[arg-type]
        fetcher=_fetcher(),
        apply=True,
        effective_date="2026-09-14",
        operation_id="issue-675",
    )

    assert result["status"] == "ok"
    assert result["repair"]["backlog"] == 1
    assert result["repair"]["selected"] == 0
    assert result["repair"]["deferred_cooldown"] == 1
    saved = read_state(state_dir, "candidate_history")
    assert saved[0]["content_description"] == THIN_TEXT


def test_named_selection_forces_cooling_repair_and_counts_it(tmp_path: Path) -> None:
    from autoscout.description_repair import record_repair_attempt

    record = _record("candidate-001", "work one|test author", THIN_TEXT, "grounded")
    record_repair_attempt(
        record,
        operation_date=OPERATION_DATE,
        operation_id="grounded-op",
        outcome="attempted_no_description",
        reason_code=REASON_FEWER_THAN_TWO,
    )
    state_dir, output_dir = _workspace(tmp_path, [record])

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        only=["work one|test author"],
        adapter=ReplayDescriptionAdjudicationAdapter(_canned({"work one|test author": RICH_TEXT})),
        fetcher=_fetcher(),
        apply=True,
        effective_date=OPERATION_DATE,
        operation_id="issue-675",
    )

    assert result["status"] == "ok"
    assert result["applied"] == 1
    saved = read_state(state_dir, "candidate_history")
    assert saved[0]["content_description"] == RICH_TEXT
    # The forced attempt counts: one cooling failure plus this success.
    assert len(saved[0][REPAIR_ATTEMPTS_FIELD]) == 2
    assert saved[0][REPAIR_ATTEMPTS_FIELD][-1]["outcome"] == "applied"


def test_exhausted_budget_is_reported(tmp_path: Path) -> None:
    history = [_record("candidate-001", "work one|test author", THIN_TEXT, "grounded")]
    state_dir, output_dir = _workspace(tmp_path, history)

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=ReplayDescriptionAdjudicationAdapter(_canned({"work one|test author": RICH_TEXT})),
        fetcher=_fetcher(),
        apply=True,
        effective_date=OPERATION_DATE,
        operation_id="issue-675",
        stage_budget_seconds=0.0,
        max_workers=1,
    )

    assert result["status"] == "ok"
    assert result["not_attempted"] == 1
    assert result["repair"]["selected"] == 1
    assert result["repair"]["attempted"] == 0
    assert result["repair"]["deferred_budget"] == 1
    saved = read_state(state_dir, "candidate_history")
    assert saved[0]["content_description"] == THIN_TEXT


def test_grounded_failure_hands_off_to_adjudication_success(tmp_path: Path) -> None:
    """Review finding on #675: grounded failures must reach adjudication.

    Three weekly grounded failures used to exhaust the record while
    adjudication selected zero items on every run. Now the first grounded
    evidence-shortfall failure hands the record to the residual route,
    which repairs it in the same weekly sequence instead of waiting out
    a cooldown grounded would then consume again.
    """
    from autoscout.grounded_description import GroundedDescriptionStageService
    from autoscout.grounded_description import StageArtifact
    from autoscout.grounded_description import StageOutcome

    record = _record("candidate-001", "work one|test author", THIN_TEXT, "grounded")
    state_dir, output_dir = _workspace(tmp_path, [record])

    class _FailingStage:
        stage_budget_seconds = 900.0
        candidate_envelope_seconds = 90.0
        discovery_budget_seconds = 10.0
        request_inactivity_timeout_seconds = 10.0
        max_workers = 1
        max_fetches = 3

        def run(self, candidates: Any, **kwargs: Any) -> StageArtifact:
            return StageArtifact(
                operation_id=str(kwargs.get("operation_id", "op")),
                operation_date=str(kwargs.get("operation_date", OPERATION_DATE)),
                outcomes=tuple(
                    StageOutcome(
                        candidate_id=str(item.get("candidate_id", "")),
                        outcome="attempted_no_description",
                        reason_code="fewer_than_two_agreeing_sources",
                        identity_key=str(item.get("identity_key", "")),
                    )
                    for item in candidates
                ),
                queue=(),
            )

    grounded = GroundedDescriptionStageService(_FailingStage())  # type: ignore[arg-type]
    enrich_result = grounded.enrich(output_dir, state_dir, OPERATION_DATE, operation_id="weekly-op")
    assert enrich_result["description_stage_report"]["repair"]["deferred_handoff"] == 0
    assert enrich_result["description_stage_report"]["repair"]["attempted"] == 1

    adjudicated = adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=ReplayDescriptionAdjudicationAdapter(_canned({"work one|test author": RICH_TEXT})),
        fetcher=_fetcher(),
        apply=True,
        effective_date=OPERATION_DATE,
        operation_id="weekly-op",
    )
    assert adjudicated["status"] == "ok"
    assert adjudicated["applied"] == 1
    assert adjudicated["repair"]["improved"] == 1
    saved = read_state(state_dir, "candidate_history")
    assert saved[0]["content_description"] == RICH_TEXT
    assert saved[0]["description_provenance"]["provenance"] == "agent-adjudicated"
    stages = [item.get("stage") for item in saved[0][REPAIR_ATTEMPTS_FIELD]]
    assert stages == ["grounded", "adjudication"]


def test_failed_outcomes_preserve_text_and_touch_nothing_else(tmp_path: Path) -> None:
    history = _thin_records()
    before = {record["identity_key"]: dict(record) for record in history}
    state_dir, output_dir = _workspace(tmp_path, history)

    class _InvalidAdapter:
        def adjudicate(self, work_item: Any) -> Any:
            return {"not": "a valid response"}

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=_InvalidAdapter(),  # type: ignore[arg-type]
        fetcher=_fetcher(),
        apply=True,
        effective_date=OPERATION_DATE,
        operation_id="issue-675",
    )

    assert result["status"] == "ok"
    saved = {record["identity_key"]: record for record in read_state(state_dir, "candidate_history")}
    for identity_key, original in before.items():
        current = saved[identity_key]
        assert current["content_description"] == original["content_description"]
        assert current["description_provenance"] == original["description_provenance"]
        for field_name in ("title", "primary_authors", "editor_status", "candidate_category"):
            assert current.get(field_name) == original.get(field_name), (identity_key, field_name)


def test_limit_prefers_untouched_repair_over_failed(tmp_path: Path) -> None:
    """Review finding on #675: --limit must see fairness order.

    With a previously-failed repair listed before an untouched one,
    limit 1 adjudicates the untouched record.
    """
    from autoscout.description_repair import record_repair_attempt as _log_attempt

    failed = _record("candidate-failed", "failed work|test author", THIN_TEXT, "grounded")
    _log_attempt(
        failed,
        operation_date="2026-09-01",
        operation_id="op-old",
        outcome="attempted_no_description",
        reason_code=REASON_FEWER_THAN_TWO,
        stage="adjudication",
    )
    untouched = _record("candidate-untouched", "untouched work|test author", THIN_TEXT, "grounded")
    state_dir, output_dir = _workspace(tmp_path, [failed, untouched])

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=ReplayDescriptionAdjudicationAdapter(
            _canned({"failed work|test author": RICH_TEXT, "untouched work|test author": RICH_TEXT})
        ),
        fetcher=_fetcher(),
        apply=False,
        effective_date=OPERATION_DATE,
        operation_id="issue-675-limit",
        limit=1,
    )

    assert result["status"] == "ok"
    preview = read_json(Path(result["preview_file"]))
    assert isinstance(preview, dict)
    rows = preview.get("preview", [])
    assert [row.get("identity_key") for row in rows] == ["untouched work|test author"]
