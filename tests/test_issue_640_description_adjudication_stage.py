"""Offline coverage for issue #640: description adjudication as a weekly stage.

The weekly workflow gains one stage named ``description-adjudication`` after
``enrichment`` and before the post-enrichment refresh. The stage reuses the
existing ``adjudicate_descriptions`` body (parallel workers, stage clock,
not-attempted reporting) through a stage-shaped adapter, applies with its
own budget and candidate cap, and never stops the run on failure.

All tests are offline and deterministic.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from adjudication_test_support import citation as _citation
from adjudication_test_support import fetcher as _fetcher_for_pages
from adjudication_test_support import page as _page
from adjudication_test_support import response as _response
from state_store_support import seed_state

from autoscout.description_adjudication import ADJUDICATION_MAX_WORKERS
from autoscout.description_adjudication import ADJUDICATION_STAGE_BUDGET_SECONDS
from autoscout.description_adjudication import DECISION_NOT_ATTEMPTED
from autoscout.description_adjudication import DescriptionAdjudicationStageService
from autoscout.description_adjudication import ReplayDescriptionAdjudicationAdapter
from autoscout.description_adjudication import adjudicate_descriptions
from autoscout.grounded_description import REASON_NOT_ATTEMPTED_STAGE_BUDGET
from autoscout.grounded_description import STAGE_BUDGET_SECONDS
from autoscout.grounded_description import BoundedHTTPError
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import NoNetworkHTTPTransport
from autoscout.grounded_description import canonicalize_url
from autoscout.weekly_workflow import ADJUDICATION_CANDIDATE_LIMIT
from autoscout.weekly_workflow import STAGE_ORDER
from autoscout.weekly_workflow import deterministic_adapters
from autoscout.weekly_workflow import run_weekly_workflow

OPERATION_DATE = "2026-09-14"
VALID_TEXT = (
    "Книга о памяти, обществе и общественной жизни маленького города. "
    "Автор соединяет репортаж с историческим контекстом и показывает, "
    "как прошлое формирует настоящее. "
    "Редакционный крючок — тихий повседневный ракурс вместо громких обобщений."
)
PUBLISHER_URL = "https://publisher.example.test/test-book"
CATALOGUE_URL = "https://catalog.example.test/test-book"


def _write_codex_fixture_config(tmp_path: Path) -> Path:
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
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


def _write_capture_fixture(tmp_path: Path, output_dir: Path) -> None:
    (tmp_path / "test-capture.json").write_text(
        json.dumps(
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


def _run_fixture_weekly(tmp_path: Path, output_name: str = "run", adapters: Any = None) -> tuple[Path, Any]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = _write_codex_fixture_config(tmp_path)
    output_dir = tmp_path / output_name
    output_dir.mkdir(exist_ok=True)
    _write_capture_fixture(tmp_path, output_dir)
    active = adapters if adapters is not None else deterministic_adapters()
    active.capture.prepare(config_path, output_dir, live=False)
    _write_extraction_fixture(output_dir)
    result = run_weekly_workflow(config_path, output_dir, adapters=active)
    return output_dir, result


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
            text=VALID_TEXT,
        )
    }


def _two_source_fetcher() -> BoundedHTTPFetcher:
    pages = {
        canonicalize_url(PUBLISHER_URL): _page(
            title="Test Book", author="Test Author", subject="A novel about memory, society and public life."
        ),
        canonicalize_url(CATALOGUE_URL): _page(
            title="Test Book", author="Test Author", subject="A novel about memory, society and public life."
        ),
    }
    return _fetcher_for_pages(pages)


# ── Stage registration ────────────────────────────────────────────────


def test_stage_order_places_adjudication_after_enrichment() -> None:
    assert "description-adjudication" in STAGE_ORDER
    enrichment_index = STAGE_ORDER.index("enrichment")
    adjudication_index = STAGE_ORDER.index("description-adjudication")
    review_index = STAGE_ORDER.index("review")
    assert adjudication_index == enrichment_index + 1
    assert adjudication_index < review_index


def test_stage_protocol_and_adapters_field_exist() -> None:
    from autoscout.weekly_workflow import DescriptionAdjudicationStageAdapter
    from autoscout.weekly_workflow import WeeklyWorkflowAdapters

    assert hasattr(DescriptionAdjudicationStageAdapter, "adjudicate_stage")
    assert "adjudication" in WeeklyWorkflowAdapters.__dataclass_fields__
    field = WeeklyWorkflowAdapters.__dataclass_fields__["adjudication"]
    assert field.default is None


def test_candidate_cap_and_stage_budget_are_own_constants() -> None:
    assert ADJUDICATION_CANDIDATE_LIMIT == 120
    assert ADJUDICATION_STAGE_BUDGET_SECONDS == 5400.0
    assert STAGE_BUDGET_SECONDS == 900.0
    assert ADJUDICATION_STAGE_BUDGET_SECONDS != STAGE_BUDGET_SECONDS


# ── Deterministic wiring is offline by construction ───────────────────


def test_deterministic_adapters_wire_replay_with_refusing_transport() -> None:
    adapters = deterministic_adapters()
    assert adapters.adjudication is not None
    service = adapters.adjudication
    assert isinstance(service, DescriptionAdjudicationStageService)
    adapter = getattr(service, "_adapter", None)
    assert isinstance(adapter, ReplayDescriptionAdjudicationAdapter)
    fetcher = getattr(service, "_fetcher", None)
    assert isinstance(fetcher, BoundedHTTPFetcher)
    assert isinstance(fetcher.transport, NoNetworkHTTPTransport)


def test_refusing_transport_rejects_every_request() -> None:
    transport = NoNetworkHTTPTransport()
    try:
        transport.request(
            "https://example.test/page",
            timeout_seconds=1.0,
            max_response_bytes=1024,
            max_redirects=3,
            allowed_hosts={"example.test"},
        )
    except BoundedHTTPError:
        return
    raise AssertionError("refusing transport must reject every request")


def test_fixture_weekly_run_never_constructs_codex_adapter_or_spawns_process(tmp_path: Path) -> None:
    import autoscout.description_adjudication as adjudication_module

    constructed: list[str] = []
    real_codex = adjudication_module.CodexDescriptionAdjudicationAdapter

    def _fail_construction(*args: Any, **kwargs: Any) -> Any:
        constructed.append("codex")
        return real_codex(*args, **kwargs)

    real_run = subprocess.run

    def _fail_spawn(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("fixture run must not spawn a subprocess")

    adjudication_module.CodexDescriptionAdjudicationAdapter = _fail_construction  # type: ignore[assignment]
    subprocess.run = _fail_spawn  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir, result = _run_fixture_weekly(Path(tmp))
            assert (output_dir / "description-adjudication-preview.json").exists()
    finally:
        adjudication_module.CodexDescriptionAdjudicationAdapter = real_codex
        subprocess.run = real_run
    assert constructed == []


def test_deterministic_wiring_makes_no_network_request(tmp_path: Path) -> None:
    class _FailingTransport:
        def request(self, url: str, **_kwargs: Any) -> Any:
            raise AssertionError(f"network request attempted for {url}")

    adapters = deterministic_adapters()
    assert adapters.adjudication is not None
    service = adapters.adjudication
    fetcher = getattr(service, "_fetcher", None)
    assert fetcher is not None
    # The wired fetcher refuses by construction; swapping in a test-failing
    # transport and running the fixture proves the run needs no fetch.
    fetcher.transport = _FailingTransport()  # type: ignore[assignment]
    with tempfile.TemporaryDirectory() as tmp:
        output_dir, result = _run_fixture_weekly(Path(tmp), adapters=adapters)
        stages = {entry["stage"]: entry["status"] for entry in result.log}
        assert stages.get("description-adjudication") == "ok"


# ── Fixture run executes the stage and fills a fallback ───────────────


def test_fixture_weekly_run_executes_stage_with_no_extra_option(tmp_path: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        output_dir, result = _run_fixture_weekly(Path(tmp))
        stages = {entry["stage"]: entry["status"] for entry in result.log}
        assert "description-adjudication" in stages
        assert stages["description-adjudication"] in {"ok", "skipped", "failed"}
        assert (output_dir / "description-adjudication-preview.json").exists()


def test_fixture_weekly_run_fills_fallback_through_stage(tmp_path: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        config_path = _write_codex_fixture_config(tmp_path)
        output_dir = tmp_path / "run"
        output_dir.mkdir()
        _write_capture_fixture(tmp_path, output_dir)
        adapters = deterministic_adapters()
        adapters.capture.prepare(config_path, output_dir, live=False)
        _write_extraction_fixture(output_dir)
        adapters.adjudication = DescriptionAdjudicationStageService(
            adapter=ReplayDescriptionAdjudicationAdapter(_canned_two_source()),
            fetcher=_two_source_fetcher(),
            limit=ADJUDICATION_CANDIDATE_LIMIT,
            stage_budget_seconds=ADJUDICATION_STAGE_BUDGET_SECONDS,
            max_workers=ADJUDICATION_MAX_WORKERS,
        )
        run_weekly_workflow(config_path, output_dir, adapters=adapters)
        history = json.loads((output_dir / "candidate-history.json").read_text(encoding="utf-8"))
        assert len(history) == 1
        assert history[0]["content_description"] == VALID_TEXT
        assert history[0]["description_provenance"]["provenance"] == "agent-adjudicated"
        preview = json.loads((output_dir / "description-adjudication-preview.json").read_text(encoding="utf-8"))
        assert preview["counts"]["applied"] == 1


def test_stage_runs_after_enrichment_and_before_refresh(tmp_path: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _output_dir, result = _run_fixture_weekly(Path(tmp))
        stages_in_order = [entry["stage"] for entry in result.log if entry.get("stage") in STAGE_ORDER]
        assert "enrichment" in stages_in_order
        assert "description-adjudication" in stages_in_order
        assert "review" in stages_in_order
        assert stages_in_order.index("description-adjudication") == stages_in_order.index("enrichment") + 1
        assert stages_in_order.index("description-adjudication") < stages_in_order.index("review")


def test_two_fixture_runs_are_byte_identical(tmp_path: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        dir_a = base / "a"
        dir_b = base / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        output_a, _ = _run_fixture_weekly(dir_a, output_name="run")
        output_b, _ = _run_fixture_weekly(dir_b, output_name="run")
        for name in ("description-adjudication-preview.json", "candidate-history.json"):
            path_a = output_a / name
            path_b = output_b / name
            assert path_a.exists() and path_b.exists()
            assert path_a.read_bytes() == path_b.read_bytes(), f"{name} differs"


# ── Budget, skipping, and failure handling ────────────────────────────


def test_stage_never_overruns_budget_with_slow_adapter(tmp_path: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # Direct stage-level proof: a slow adapter plus a fake clock shows
        # the stage returns inside the budget with the rest not attempted.
        state_dir = tmp_path / "state"
        output_dir = tmp_path / "run"
        state_dir.mkdir()
        output_dir.mkdir()
        keys = [f"work {index}|alex author" for index in range(4)]
        seed_state(
            state_dir,
            candidate_history=[
                {
                    "candidate_id": f"candidate-{index + 1:03d}",
                    "identity_key": key,
                    "title": "Work",
                    "primary_authors": ["Alex Author"],
                    "content_description": "",
                }
                for index, key in enumerate(keys)
            ],
        )
        seed_state(state_dir, candidate_visibility=[])

        class _SlowAdapter:
            def adjudicate(self, work_item: Any) -> Any:
                now[0] += 100.0
                return {
                    "identity_key": str(work_item.get("identity_key", "")),
                    "resolved": True,
                    "unresolved_reason": "",
                    "confidence": "high",
                    "content_description": VALID_TEXT,
                    "citations": [],
                }

        now = [1000.0]

        def _fake_monotonic() -> float:
            return now[0]

        class _UnreachableTransport:
            def request(self, url: str, **_kwargs: Any) -> Any:
                raise BoundedHTTPError("offline test never fetches")

        result = adjudicate_descriptions(
            state_dir,
            output_dir,
            adapter=_SlowAdapter(),  # type: ignore[arg-type]
            fetcher=BoundedHTTPFetcher(transport=_UnreachableTransport()),  # type: ignore[arg-type]
            apply=False,
            stage_budget_seconds=150.0,
            max_workers=1,
            monotonic=_fake_monotonic,
        )
        assert result["not_attempted"] >= 1
        preview = json.loads((output_dir / "description-adjudication-preview.json").read_text(encoding="utf-8"))
        reasons = {row["decision_reason"] for row in preview["preview"]}
        assert REASON_NOT_ATTEMPTED_STAGE_BUDGET in reasons


def test_spent_budget_through_the_workflow_seam_still_reaches_review_and_health() -> None:
    """A stage that runs out of clock reports it and does not stop the run.

    The stage-level test above proves the budget bounds the work. This one
    proves what that means for the editor: the weekly run still renders the
    review and validates health, and the stage says what it did not attempt
    instead of failing the run.
    """

    class _NeverCalledAdapter:
        def adjudicate(self, work_item: Any) -> Any:
            raise AssertionError("a spent budget must admit no candidate")

    def _stopped_clock() -> float:
        return 1000.0

    adapters = deterministic_adapters()
    adapters.adjudication = DescriptionAdjudicationStageService(
        adapter=_NeverCalledAdapter(),  # type: ignore[arg-type]
        fetcher=BoundedHTTPFetcher(transport=NoNetworkHTTPTransport()),
        limit=ADJUDICATION_CANDIDATE_LIMIT,
        # Smaller than one per-call reservation, so admission stops before
        # the first candidate and the clock never has to move.
        stage_budget_seconds=1.0,
        max_workers=ADJUDICATION_MAX_WORKERS,
        monotonic=_stopped_clock,
    )
    with tempfile.TemporaryDirectory() as tmp:
        output_dir, result = _run_fixture_weekly(Path(tmp), adapters=adapters)
        stages = {entry["stage"]: entry["status"] for entry in result.log}
        assert stages.get("description-adjudication") == "ok"
        assert stages.get("review") == "ok"
        assert stages.get("health") == "ok"
        preview = json.loads((output_dir / "description-adjudication-preview.json").read_text(encoding="utf-8"))
        assert preview["counts"]["not_attempted"] >= 1
        reasons = {row["decision_reason"] for row in preview["preview"]}
        assert REASON_NOT_ATTEMPTED_STAGE_BUDGET in reasons


def test_unholdable_candidate_is_not_attempted_not_dropped(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "run"
    state_dir.mkdir()
    output_dir.mkdir()
    seed_state(
        state_dir,
        candidate_history=[
            {
                "candidate_id": "candidate-001",
                "identity_key": "work one|alex author",
                "title": "Work",
                "primary_authors": ["Alex Author"],
                "content_description": "",
            }
        ],
    )
    seed_state(state_dir, candidate_visibility=[])

    class _NeverCalledAdapter:
        def __init__(self) -> None:
            self.calls = 0

        def adjudicate(self, work_item: Any) -> Any:
            self.calls += 1
            raise AssertionError("budget cannot hold even one call")

    adapter = _NeverCalledAdapter()

    class _UnreachableTransport:
        def request(self, url: str, **_kwargs: Any) -> Any:
            raise BoundedHTTPError("offline test never fetches")

    result = adjudicate_descriptions(
        state_dir,
        output_dir,
        adapter=adapter,  # type: ignore[arg-type]
        fetcher=BoundedHTTPFetcher(transport=_UnreachableTransport()),  # type: ignore[arg-type]
        apply=False,
        stage_budget_seconds=1.0,
        max_workers=1,
        monotonic=lambda: 1000.0,
    )
    assert result["previewed"] == 1
    assert result["not_attempted"] == 1
    assert adapter.calls == 0
    preview = json.loads((output_dir / "description-adjudication-preview.json").read_text(encoding="utf-8"))
    (row,) = preview["preview"]
    assert row["decision"] == DECISION_NOT_ATTEMPTED
    assert row["decision_reason"] == REASON_NOT_ATTEMPTED_STAGE_BUDGET


def test_no_adjudication_adapter_skips_stage_and_completes(tmp_path: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        config_path = _write_codex_fixture_config(tmp_path)
        output_dir = tmp_path / "run"
        output_dir.mkdir()
        _write_capture_fixture(tmp_path, output_dir)
        adapters = deterministic_adapters()
        adapters.adjudication = None
        adapters.capture.prepare(config_path, output_dir, live=False)
        _write_extraction_fixture(output_dir)
        result = run_weekly_workflow(config_path, output_dir, adapters=adapters)
        stages = {entry["stage"]: entry["status"] for entry in result.log}
        assert stages.get("description-adjudication") == "skipped"
        assert stages.get("review") == "ok"
        assert stages.get("health") == "ok"


def test_stage_exception_is_logged_failed_and_run_continues(tmp_path: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        config_path = _write_codex_fixture_config(tmp_path)
        output_dir = tmp_path / "run"
        output_dir.mkdir()
        _write_capture_fixture(tmp_path, output_dir)
        adapters = deterministic_adapters()

        class _BoomAdapter:
            def adjudicate_stage(
                self,
                output_dir: Path,
                state_dir: Path | None = None,
                effective_date: str | None = None,
                *,
                operation_id: str | None = None,
                state_store: Any = None,
            ) -> dict[str, Any]:
                raise RuntimeError("boom with /secret/path and key")

        adapters.adjudication = _BoomAdapter()  # type: ignore[assignment]
        adapters.capture.prepare(config_path, output_dir, live=False)
        _write_extraction_fixture(output_dir)
        result = run_weekly_workflow(config_path, output_dir, adapters=adapters)
        stages = {entry["stage"]: entry["status"] for entry in result.log}
        assert stages.get("description-adjudication") == "failed"
        assert stages.get("review") == "ok"
        assert stages.get("health") == "ok"
        failed_entry = next(entry for entry in result.log if entry.get("stage") == "description-adjudication")
        detail = failed_entry.get("detail", {})
        assert detail.get("reason") == "stage_execution_failure"
        assert "boom" not in json.dumps(detail)
        assert "/secret/path" not in json.dumps(detail)


# ── Presentation ──────────────────────────────────────────────────────


def test_russian_progress_line_is_russian_not_fallback() -> None:
    from autoscout.russian_workflow import _PROGRESS_STAGES
    from autoscout.russian_workflow import stage_message

    assert "description-adjudication" in _PROGRESS_STAGES
    message = stage_message("description-adjudication")
    assert message != "Выполняю этап: description-adjudication..."
    assert any("А" <= character <= "я" for character in message)
