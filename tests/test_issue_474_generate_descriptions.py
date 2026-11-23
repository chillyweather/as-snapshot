"""Offline coverage for issue #474 standalone description generation."""

from __future__ import annotations

import csv
import json
from copy import deepcopy
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer
from pathlib import Path
from threading import Lock
from threading import Thread
from typing import Any
from typing import ClassVar
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import pytest
from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout.__main__ import main
from autoscout.generate_descriptions import ATTEMPT_FIELD
from autoscout.generate_descriptions import PREFLIGHT_ADAPTER_NO_RESPONSE
from autoscout.generate_descriptions import PREFLIGHT_ENDPOINT_NOT_CONFIGURED
from autoscout.generate_descriptions import DescriptionGenerationPreflightError
from autoscout.generate_descriptions import generate_descriptions
from autoscout.grounded_description import PREFLIGHT_ADAPTER_KEY_MISSING
from autoscout.grounded_description import PREFLIGHT_ADAPTER_NOT_RUNNING
from autoscout.grounded_description import PREFLIGHT_ADAPTER_VENDOR_ERROR
from autoscout.grounded_description import AcceptedSource
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import GroundedDescriptionStageService
from autoscout.grounded_description import HTTPDiscoveryProvider
from autoscout.grounded_description import QueueEntry
from autoscout.grounded_description import StageArtifact
from autoscout.grounded_description import StageOutcome
from autoscout.grounded_description import StandardLibraryHTTPTransport
from autoscout.grounded_description import SynthesisResult
from autoscout.grounded_description import validate_stage_artifact
from autoscout.grounded_search_adapter import API_KEY_ENV
from autoscout.grounded_search_adapter import SearchAdapterError
from autoscout.grounded_search_adapter import _Handler
from autoscout.json_io import read_json
from autoscout.review_rendering import render_review
from autoscout.vendor_rate_limit import VendorRateLimiter
from autoscout.weekly_report import WeeklyReportStore
from autoscout.weekly_report_row import editor_row_from_candidate
from autoscout.weekly_workflow import _make_production_enrichment

FIXTURE = Path(__file__).parent / "fixtures" / "issue-474" / "history.json"


class FakeDiscovery:
    def __init__(self, plan: dict[str, str] | None = None) -> None:
        self.plan = plan or {}
        self.calls: list[str] = []

    def discover(self, candidate: dict[str, Any]) -> list[dict[str, Any]]:
        candidate_id = str(candidate["candidate_id"])
        self.calls.append(candidate_id)
        if self.plan.get(candidate_id, "grounded") == "none":
            return []
        count = 1 if self.plan.get(candidate_id) == "single" else 2
        return [
            {
                "url": f"https://source-{index}.example.test/{candidate_id}",
                "source_kind": "review" if index == 0 else "publisher",
                "source_family": f"family-{index}",
                "owner_id": f"owner-{index}-{candidate_id}",
                "classification": "classified",
                "title": candidate["title"],
                "primary_authors": candidate["primary_authors"],
                "subject_text": "Память, миграция и общественная жизнь.",
            }
            for index in range(count)
        ]


class FakeFetch:
    def fetch(self, result: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "url": result.url,
            "source_kind": result.source_kind,
            "source_family": result.source_family,
            "owner_id": result.owner_id,
            "source_language": "en",
            "title": result.title,
            "primary_authors": list(result.primary_authors),
            "subject_text": "Память, миграция и общественная жизнь.",
            "checked_date": kwargs.get("checked_date", "2026-01-10"),
            "classification": "classified",
        }


class FakeSynthesizer:
    def synthesize(self, candidate: dict[str, Any], sources: Any) -> SynthesisResult:
        return SynthesisResult(
            f"Это русское описание книги {candidate['title']} о памяти и миграции. "
            "Второй фрагмент раскрывает документальную основу, называет проверенные "
            "страницы и уточняет редакционный ракурс книги. "
            "Третье предложение закрепляет вывод и показывает, почему книга попала в обзор."
        )


class _OldAdapterHandler(BaseHTTPRequestHandler):
    queries: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            self._reply(404, {"error": "unknown path"})
            return
        if parsed.path != "/search":
            self._reply(404, {"error": "unknown path"})
            return
        query = (parse_qs(parsed.query).get("q") or [""])[0]
        type(self).queries.append(query)
        self._reply(200, {"results": []})

    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


def _start_http_server(handler: type[BaseHTTPRequestHandler]) -> tuple[HTTPServer, Thread]:
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop_http_server(server: HTTPServer, thread: Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join()


def _real_provider(server: HTTPServer) -> HTTPDiscoveryProvider:
    return HTTPDiscoveryProvider(
        f"http://127.0.0.1:{server.server_port}/search?q={{query}}",
        transport=StandardLibraryHTTPTransport(),
    )


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _history() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _workspace(tmp_path: Path, history: list[dict[str, Any]] | None = None) -> tuple[Path, Path]:
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    state_dir.mkdir()
    output_dir.mkdir()
    seed_state(state_dir, candidate_history=history if history is not None else _history())
    return state_dir, output_dir


def _service(
    plan: dict[str, str] | None = None, *, stage_budget_seconds: float = 900.0
) -> tuple[GroundedDescriptionStageService, FakeDiscovery]:
    discovery = FakeDiscovery(plan)
    return (
        GroundedDescriptionStageService(
            GroundedDescriptionStage(
                discovery=discovery,
                fetch=FakeFetch(),
                synthesizer=FakeSynthesizer(),
                stage_budget_seconds=stage_budget_seconds,
            )
        ),
        discovery,
    )


def _write_coherent_report(output_dir: Path, candidate: dict[str, Any], report_date: str) -> None:
    store = WeeklyReportStore(output_dir)
    store.write_json("pilot-status.json", {"run_date": report_date, "ready_for_editor": True})
    store.write_json(
        "run-summary.json",
        {"run_date": report_date, "editor_facing_count": 1, "raw_candidate_count": 1},
    )
    store.write_json("source-runs.json", [{"source_name": "fixture", "source_check_date": report_date}])
    store.write_json("source-mentions.json", [{"title": candidate["title"], "source_check_date": report_date}])
    store.write_json("scouting-candidates.json", [deepcopy(candidate)])
    store.write_editor_csv([editor_row_from_candidate(candidate)])


def _run(
    state_dir: Path,
    output_dir: Path,
    service: GroundedDescriptionStageService,
    **kwargs: Any,
) -> dict[str, Any]:
    return generate_descriptions(
        state_dir,
        output_dir,
        service=service,
        preflight=lambda: True,
        effective_date="2026-01-10",
        **kwargs,
    )


def test_four_provenance_cases_skip_only_exact_grounded(tmp_path: Path) -> None:
    # After #548 a grounded candidate is queued when its freshness cannot be
    # established. c-grounded carries an evidence_basis but no attempt
    # history, so there is no date to compare against
    # GROUNDED_REVERIFY_AGE_DAYS and it must be verified rather than assumed
    # current. Its discovered evidence then differs from the stored basis, so
    # it is re-synthesised. Both halves are the honest expectation: unknown
    # freshness earns a check, and changed evidence earns a new description.
    state_dir, output_dir = _workspace(tmp_path)
    service, discovery = _service({"c-codex": "none", "c-backfilled": "none", "c-blank": "none"})

    _run(state_dir, output_dir, service, max_workers=1)

    # Standalone plans fresh work before repairs, repairs
    # fewest-attempts-first (#674): c-blank is fresh so it leads, and the
    # three repair candidates follow in candidate-id order (all
    # untouched). The product rule under test is which candidates run,
    # not history order.
    assert discovery.calls == ["c-blank", "c-backfilled", "c-codex", "c-grounded"]
    saved = {record["candidate_id"]: record for record in read_state(state_dir, "candidate_history")}
    # c-grounded is now attempted because its evidence changed (old vs new URLs)
    assert len(saved["c-grounded"][ATTEMPT_FIELD]) == 1
    assert saved["c-grounded"][ATTEMPT_FIELD][0]["outcome"] in {"grounded", "single_source"}
    assert all(len(record[ATTEMPT_FIELD]) == 1 for key, record in saved.items() if key != "c-grounded")


def test_grounded_effect_replaces_description_and_preserves_annotation(tmp_path: Path) -> None:
    history = [_history()[1]]
    state_dir, output_dir = _workspace(tmp_path, history)
    service, _ = _service()
    original_short = history[0]["short_description"]

    _run(state_dir, output_dir, service)

    saved = read_state(state_dir, "candidate_history")[0]
    assert saved["grounded_outcome"] == "grounded"
    assert saved["description_provenance"]["provenance"] == "grounded"
    assert len(saved["description_provenance"]["evidence_basis"]) == 2
    assert saved[ATTEMPT_FIELD][0]["reason_code"] == ""
    assert saved["short_description"] == original_short


def test_single_source_effect_is_persisted_and_visible(tmp_path: Path) -> None:
    history = [_history()[1]]
    state_dir, output_dir = _workspace(tmp_path, history)
    service, _ = _service({"c-codex": "single"})

    _run(state_dir, output_dir, service)

    saved = read_state(state_dir, "candidate_history")[0]
    assert saved["grounded_outcome"] == "single_source"
    assert saved["description_provenance"]["provenance"] == "grounded"
    assert saved[ATTEMPT_FIELD][0]["reason_code"] == ""
    with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["Description status"].startswith("⚠")
    (output_dir / "grounded-description-stage.json").unlink()
    assert render_review(output_dir, state_dir=state_dir, effective_date="2026-01-10") == 0
    with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
        persisted_row = next(csv.DictReader(handle))
    assert persisted_row["Description status"].startswith("⚠")


def test_attempted_no_description_preserves_serialized_description_fields(tmp_path: Path) -> None:
    history = [_history()[1]]
    state_dir, output_dir = _workspace(tmp_path, history)
    before = deepcopy(history[0])
    service, _ = _service({"c-codex": "none"})

    _run(state_dir, output_dir, service)

    after = read_state(state_dir, "candidate_history")[0]
    for field in ("content_description", "description_provenance"):
        assert json.dumps(after[field], ensure_ascii=False, sort_keys=True) == json.dumps(
            before[field], ensure_ascii=False, sort_keys=True
        )
    assert len(after[ATTEMPT_FIELD]) == 1


def test_not_attempted_changes_no_history_and_has_scheduling_metadata(tmp_path: Path) -> None:
    history = [_history()[3]]
    state_dir, output_dir = _workspace(tmp_path, history)
    before = state_snapshot(state_dir)
    service, _ = _service(stage_budget_seconds=0.0)

    _run(state_dir, output_dir, service)

    assert state_snapshot(state_dir) == before
    artifact = read_json(output_dir / "grounded-description-stage.json")
    assert artifact["outcomes"][0]["outcome"] == "not_attempted"
    assert "scheduling" in artifact["outcomes"][0]
    assert validate_stage_artifact(artifact)


@pytest.mark.parametrize("age_days,eligible", [(3, False), (8, True)])
def test_failed_retry_age_three_and_eight_days(tmp_path: Path, age_days: int, eligible: bool) -> None:
    record = _history()[3]
    record[ATTEMPT_FIELD] = [
        {"date": f"2026-01-{10 - age_days:02d}", "outcome": "attempted_no_description", "reason_code": "no_results"}
    ]
    state_dir, output_dir = _workspace(tmp_path, [record])
    service, discovery = _service({"c-blank": "none"})

    _run(state_dir, output_dir, service)

    assert bool(discovery.calls) is eligible


def test_retry_failed_overrides_recent_failure(tmp_path: Path) -> None:
    record = _history()[3]
    record[ATTEMPT_FIELD] = [{"date": "2026-01-07", "outcome": "attempted_no_description", "reason_code": "no_results"}]
    state_dir, output_dir = _workspace(tmp_path, [record])
    service, discovery = _service({"c-blank": "none"})

    _run(state_dir, output_dir, service, retry_failed=True)

    assert discovery.calls == ["c-blank"]


def test_limit_twenty_selects_exactly_twenty_candidates(tmp_path: Path) -> None:
    records = []
    for index in range(25):
        records.append(
            {
                "candidate_id": f"limit-{index:02d}",
                "identity_key": f"Limit Work {index}|Alex Author",
                "title": f"Limit Work {index}",
                "primary_authors": ["Alex Author"],
                "content_description": "",
                "short_description": f"Annotation {index}",
            }
        )
    state_dir, output_dir = _workspace(tmp_path, records)
    service, discovery = _service({record["candidate_id"]: "none" for record in records})

    result = _run(state_dir, output_dir, service, limit=20)

    assert result["eligible_count"] == 20
    assert len(discovery.calls) == 20
    saved = read_state(state_dir, "candidate_history")
    assert sum(ATTEMPT_FIELD in record for record in saved) == 20


def test_limit_must_be_positive(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path)
    service, _ = _service()
    with pytest.raises(ValueError, match="positive"):
        _run(state_dir, output_dir, service, limit=0)


def test_only_selects_one_exact_identity_key(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path)
    service, discovery = _service({"c-codex": "none"})

    _run(state_dir, output_dir, service, only=["Codex Work|Alex Author"])

    assert discovery.calls == ["c-codex"]


def test_only_selects_two_exact_identity_keys_in_input_order(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path)
    service, discovery = _service({"c-codex": "none", "c-blank": "none"})

    _run(
        state_dir,
        output_dir,
        service,
        only=["Blank Work|Alex Author", "Codex Work|Alex Author"],
        max_workers=1,
    )

    assert discovery.calls == ["c-blank", "c-codex"]


def test_only_attempts_grounded_candidate(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path)
    service, discovery = _service({"c-grounded": "none"})

    _run(state_dir, output_dir, service, only=["grounded work|alex author"])

    assert discovery.calls == ["c-grounded"]


def test_only_attempts_recent_failure_without_retry_failed(tmp_path: Path) -> None:
    record = _history()[3]
    record[ATTEMPT_FIELD] = [{"date": "2026-01-09", "outcome": "attempted_no_description", "reason_code": "no_results"}]
    state_dir, output_dir = _workspace(tmp_path, [record])
    service, discovery = _service({"c-blank": "none"})

    _run(state_dir, output_dir, service, only=["Blank Work|Alex Author"])

    assert discovery.calls == ["c-blank"]


def test_unmatched_only_key_fails_before_calls_or_writes(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path)
    history_before = state_snapshot(state_dir)
    sentinel = output_dir / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    service, discovery = _service()

    with pytest.raises(ValueError, match="Missing Work\\|Alex Author"):
        _run(state_dir, output_dir, service, only=["Missing Work|Alex Author"])

    assert discovery.calls == []
    assert state_snapshot(state_dir) == history_before
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_matched_and_unmatched_only_keys_fail_atomically(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path)
    history_before = state_snapshot(state_dir)
    sentinel = output_dir / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    service, discovery = _service()

    with pytest.raises(ValueError, match="Missing Work\\|Alex Author"):
        _run(
            state_dir,
            output_dir,
            service,
            only=["Codex Work|Alex Author", "Missing Work|Alex Author"],
        )

    assert discovery.calls == []
    assert state_snapshot(state_dir) == history_before
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_only_matches_case_and_surrounding_whitespace_only(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path)
    service, discovery = _service({"c-codex": "none"})

    _run(state_dir, output_dir, service, only=["  CODEX WORK|ALEX AUTHOR  "])

    assert discovery.calls == ["c-codex"]


def test_duplicate_named_keys_are_attempted_once(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path)
    only_file = tmp_path / "only.txt"
    only_file.write_text("codex work|alex author\n", encoding="utf-8")
    service, discovery = _service({"c-codex": "none"})

    _run(
        state_dir,
        output_dir,
        service,
        only=["Codex Work|Alex Author", "  CODEX WORK|ALEX AUTHOR  "],
        only_file=only_file,
    )

    assert discovery.calls == ["c-codex"]


def test_only_and_only_file_combine_in_stable_order(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path)
    only_file = tmp_path / "only.txt"
    only_file.write_text("Blank Work|Alex Author\nCodex Work|Alex Author\n", encoding="utf-8")
    service, discovery = _service({"c-codex": "none", "c-blank": "none"})

    _run(
        state_dir,
        output_dir,
        service,
        only=["Codex Work|Alex Author"],
        only_file=only_file,
        max_workers=1,
    )

    assert discovery.calls == ["c-codex", "c-blank"]


def test_only_file_ignores_blank_lines_and_comments(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path)
    only_file = tmp_path / "only.txt"
    only_file.write_text(
        "\n# ignore this\n  # and this too  \nCodex Work|Alex Author\n",
        encoding="utf-8",
    )
    service, discovery = _service({"c-codex": "none"})

    _run(state_dir, output_dir, service, only_file=only_file)

    assert discovery.calls == ["c-codex"]


def test_empty_only_selection_fails_before_calls_or_writes(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path)
    history_before = state_snapshot(state_dir)
    sentinel = output_dir / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    service, discovery = _service()

    with pytest.raises(ValueError, match="selection is empty"):
        _run(state_dir, output_dir, service, only=[""])

    assert discovery.calls == []
    assert state_snapshot(state_dir) == history_before
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_empty_only_file_selection_fails_before_calls_or_writes(tmp_path: Path) -> None:
    state_dir, output_dir = _workspace(tmp_path)
    history_before = state_snapshot(state_dir)
    sentinel = output_dir / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    only_file = tmp_path / "only.txt"
    only_file.write_text("\n# ignore\n  # ignore too\n", encoding="utf-8")
    service, discovery = _service()

    with pytest.raises(ValueError, match="selection is empty"):
        _run(state_dir, output_dir, service, only_file=only_file)

    assert discovery.calls == []
    assert state_snapshot(state_dir) == history_before
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_ambiguous_named_key_fails_with_all_candidate_ids(tmp_path: Path) -> None:
    history = [
        {
            "candidate_id": "ambiguous-one",
            "identity_key": "Duplicate Work|Alex Author",
            "title": "Duplicate Work",
            "primary_authors": ["Alex Author"],
            "content_description": "",
        },
        {
            "candidate_id": "ambiguous-two",
            "identity_key": " duplicate work|alex author ",
            "title": "Duplicate Work",
            "primary_authors": ["Alex Author"],
            "content_description": "",
        },
    ]
    state_dir, output_dir = _workspace(tmp_path, history)
    history_before = state_snapshot(state_dir)
    service, discovery = _service()

    with pytest.raises(ValueError, match="ambiguous-two.*ambiguous-one"):
        _run(state_dir, output_dir, service, only=["DUPLICATE WORK|ALEX AUTHOR"])

    assert discovery.calls == []
    assert state_snapshot(state_dir) == history_before


def test_ambiguous_named_key_labels_missing_candidate_id_stably(tmp_path: Path) -> None:
    history = [
        {
            "candidate_id": None,
            "identity_key": "Duplicate Work|Alex Author",
            "title": "Duplicate Work",
            "primary_authors": ["Alex Author"],
            "content_description": "",
        },
        {
            "candidate_id": None,
            "identity_key": " duplicate work|alex author ",
            "title": "Duplicate Work",
            "primary_authors": ["Alex Author"],
            "content_description": "",
        },
    ]
    state_dir, output_dir = _workspace(tmp_path, history)
    service, discovery = _service()

    with pytest.raises(
        ValueError, match="<missing candidate_id at history index 0>.*<missing candidate_id at history index 1>"
    ):
        _run(state_dir, output_dir, service, only=["DUPLICATE WORK|ALEX AUTHOR"])

    assert discovery.calls == []


@pytest.mark.parametrize("selector", ["only", "only_file"])
def test_limit_cannot_be_combined_with_named_selection(tmp_path: Path, selector: str) -> None:
    state_dir, output_dir = _workspace(tmp_path)
    only_file = tmp_path / "only.txt"
    only_file.write_text("Codex Work|Alex Author\n", encoding="utf-8")
    service, discovery = _service()
    kwargs = {selector: ["Codex Work|Alex Author"] if selector == "only" else only_file}

    with pytest.raises(ValueError, match="^--limit cannot be combined with --only or --only-file$"):
        _run(state_dir, output_dir, service, limit=1, **kwargs)

    assert discovery.calls == []


def test_cli_limit_and_only_returns_clear_error_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    state_dir, output_dir = _workspace(tmp_path)
    service, _ = _service()
    monkeypatch.setattr(service, "preflight", lambda: True)
    monkeypatch.setattr("autoscout.weekly_workflow._make_production_enrichment", lambda **kwargs: service)

    assert (
        main(
            [
                "generate-descriptions",
                "--state-dir",
                str(state_dir),
                "--output-dir",
                str(output_dir),
                "--limit",
                "1",
                "--only",
                "Codex Work|Alex Author",
            ]
        )
        == 1
    )
    assert (
        capsys.readouterr().out.strip()
        == "generate-descriptions: --limit cannot be combined with --only or --only-file"
    )


def test_cli_limit_and_only_file_returns_clear_error_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    state_dir, output_dir = _workspace(tmp_path)
    only_file = tmp_path / "only.txt"
    only_file.write_text("Codex Work|Alex Author\n", encoding="utf-8")
    service, _ = _service()
    monkeypatch.setattr(service, "preflight", lambda: True)
    monkeypatch.setattr("autoscout.weekly_workflow._make_production_enrichment", lambda **kwargs: service)

    assert (
        main(
            [
                "generate-descriptions",
                "--state-dir",
                str(state_dir),
                "--output-dir",
                str(output_dir),
                "--limit",
                "1",
                "--only-file",
                str(only_file),
            ]
        )
        == 1
    )
    assert (
        capsys.readouterr().out.strip()
        == "generate-descriptions: --limit cannot be combined with --only or --only-file"
    )


@pytest.mark.parametrize("selector", ["empty_only", "missing_only_file"])
def test_preflight_failure_wins_over_invalid_selection(tmp_path: Path, selector: str) -> None:
    state_dir, output_dir = _workspace(tmp_path)

    class NoResponse:
        def preflight(self) -> bool:
            return False

    service = GroundedDescriptionStageService(GroundedDescriptionStage(discovery=NoResponse()))
    kwargs = {"only": [""]} if selector == "empty_only" else {"only_file": tmp_path / "missing-only.txt"}

    with pytest.raises(DescriptionGenerationPreflightError) as error:
        generate_descriptions(state_dir, output_dir, service=service, **kwargs)

    assert error.value.reason_code == PREFLIGHT_ADAPTER_NO_RESPONSE


def test_cli_only_passes_repeated_exact_keys_to_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir, output_dir = _workspace(tmp_path)
    service, discovery = _service({"c-codex": "none", "c-blank": "none"})
    monkeypatch.setattr(service, "preflight", lambda: True)
    monkeypatch.setattr("autoscout.weekly_workflow._make_production_enrichment", lambda **kwargs: service)

    assert (
        main(
            [
                "generate-descriptions",
                "--state-dir",
                str(state_dir),
                "--output-dir",
                str(output_dir),
                "--only",
                "Codex Work|Alex Author",
                "--only",
                "Blank Work|Alex Author",
            ]
        )
        == 0
    )

    assert set(discovery.calls) == {"c-codex", "c-blank"}
    assert len(discovery.calls) == 2


def test_cli_help_explains_explicit_named_selection_overrides_skip_rules(
    capsys: Any,
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["generate-descriptions", "--help"])

    assert exit_info.value.code == 0
    help_output = capsys.readouterr().out.lower()
    assert "explicit named selection" in help_output
    assert "already have a grounded description" in help_output
    assert "recent failure" in help_output


def test_preflight_endpoint_unset_has_distinct_message_and_no_history_mutation(tmp_path: Path) -> None:
    history = _history()
    state_dir, output_dir = _workspace(tmp_path, history)
    before = state_snapshot(state_dir)
    service = _make_production_enrichment(discovery_endpoint=None)

    with pytest.raises(DescriptionGenerationPreflightError) as error:
        generate_descriptions(state_dir, output_dir, service=service)

    assert error.value.reason_code == PREFLIGHT_ENDPOINT_NOT_CONFIGURED
    assert state_snapshot(state_dir) == before


def test_cli_reports_unset_endpoint_and_preserves_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    state_dir, output_dir = _workspace(tmp_path)
    before = state_snapshot(state_dir)
    monkeypatch.delenv("AUTOSCOUT_GROUNDED_DISCOVERY_ENDPOINT", raising=False)

    assert main(["generate-descriptions", "--state-dir", str(state_dir), "--output-dir", str(output_dir)]) == 2
    assert capsys.readouterr().out.strip() == "generate-descriptions: discovery endpoint is not configured"
    assert state_snapshot(state_dir) == before


def test_real_adapter_cli_reports_distinct_key_and_stopped_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    state_dir, output_dir = _workspace(tmp_path)
    before = state_snapshot(state_dir)
    server, thread = _start_http_server(_Handler)
    endpoint = f"http://127.0.0.1:{server.server_port}/search?q={{query}}"
    monkeypatch.setenv("AUTOSCOUT_GROUNDED_DISCOVERY_ENDPOINT", endpoint)
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    try:
        assert main(["generate-descriptions", "--state-dir", str(state_dir), "--output-dir", str(output_dir)]) == 2
        key_output = capsys.readouterr().out.strip().splitlines()
        key_message = key_output[-1]
        assert key_message == "generate-descriptions: AUTOSCOUT_SEARCH_API_KEY is not set"
        assert state_snapshot(state_dir) == before

        _stop_http_server(server, thread)
        assert main(["generate-descriptions", "--state-dir", str(state_dir), "--output-dir", str(output_dir)]) == 2
        stopped_message = capsys.readouterr().out.strip()
        assert stopped_message == "generate-descriptions: discovery adapter is not running"
        assert stopped_message != key_message
        assert state_snapshot(state_dir) == before

        monkeypatch.delenv("AUTOSCOUT_GROUNDED_DISCOVERY_ENDPOINT", raising=False)
        assert main(["generate-descriptions", "--state-dir", str(state_dir), "--output-dir", str(output_dir)]) == 2
        endpoint_message = capsys.readouterr().out.strip()
        assert endpoint_message == "generate-descriptions: discovery endpoint is not configured"
        assert len({key_message, stopped_message, endpoint_message}) == 3
        assert state_snapshot(state_dir) == before
    finally:
        if thread.is_alive():
            _stop_http_server(server, thread)


def test_real_adapter_health_is_vendor_free_and_reports_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    server, thread = _start_http_server(_Handler)
    monkeypatch.setenv(API_KEY_ENV, "dummy-test-key")
    try:

        def fail_vendor(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("health preflight must not call the vendor")

        monkeypatch.setattr("autoscout.grounded_search_adapter.brave_search", fail_vendor)
        result = _real_provider(server).preflight()
        assert result.ok is True
        assert result.reason_code not in {
            PREFLIGHT_ADAPTER_KEY_MISSING,
            PREFLIGHT_ADAPTER_NOT_RUNNING,
            PREFLIGHT_ADAPTER_VENDOR_ERROR,
        }
    finally:
        _stop_http_server(server, thread)


def test_real_adapter_health_uses_compatibility_search_for_old_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    _OldAdapterHandler.queries = []
    server, thread = _start_http_server(_OldAdapterHandler)
    try:
        result = _real_provider(server).preflight()
        assert result.ok is True
        assert _OldAdapterHandler.queries == ["autoscout preflight"]
    finally:
        _stop_http_server(server, thread)


def test_compatibility_preflight_waits_before_its_vendor_request() -> None:
    _OldAdapterHandler.queries = []
    server, thread = _start_http_server(_OldAdapterHandler)
    clock = _FakeClock()
    limiter = VendorRateLimiter(
        min_interval_seconds=1.0,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )
    limiter.wait()
    clock.now = 0.25
    provider = HTTPDiscoveryProvider(
        f"http://127.0.0.1:{server.server_port}/search?q={{query}}",
        transport=StandardLibraryHTTPTransport(),
        rate_limiter=limiter,
    )
    try:
        result = provider.preflight()
        assert result.ok is True
        assert clock.slept == [0.75]
        assert _OldAdapterHandler.queries == ["autoscout preflight"]
    finally:
        _stop_http_server(server, thread)


def test_real_adapter_vendor_error_is_preserved_without_vendor_network(monkeypatch: pytest.MonkeyPatch) -> None:
    class LegacyAdapterHandler(_Handler):
        def do_GET(self) -> None:
            if urlsplit(self.path).path == "/health":
                self._reply(404, {"error": "unknown path"})
                return
            super().do_GET()

    server, thread = _start_http_server(LegacyAdapterHandler)
    monkeypatch.setenv(API_KEY_ENV, "dummy-test-key")

    def fail_vendor(*args: Any, **kwargs: Any) -> Any:
        raise SearchAdapterError("search vendor rejected request")

    monkeypatch.setattr("autoscout.grounded_search_adapter.brave_search", fail_vendor)
    try:
        result = _real_provider(server).preflight()
        assert result.ok is False
        assert result.reason_code == PREFLIGHT_ADAPTER_VENDOR_ERROR
        assert result.message == "search vendor rejected request"
    finally:
        _stop_http_server(server, thread)


def test_preflight_adapter_no_response_has_distinct_reason_and_no_history_mutation(tmp_path: Path) -> None:
    history = _history()
    state_dir, output_dir = _workspace(tmp_path, history)
    before = state_snapshot(state_dir)

    class NoResponse:
        def preflight(self) -> bool:
            return False

    service = GroundedDescriptionStageService(GroundedDescriptionStage(discovery=NoResponse()))
    with pytest.raises(DescriptionGenerationPreflightError) as error:
        generate_descriptions(state_dir, output_dir, service=service)

    assert error.value.reason_code == PREFLIGHT_ADAPTER_NO_RESPONSE
    assert state_snapshot(state_dir) == before


def test_cli_reports_adapter_no_response_without_mutating_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    state_dir, output_dir = _workspace(tmp_path)
    before = state_snapshot(state_dir)

    class NoResponse:
        def preflight(self) -> bool:
            return False

    service = GroundedDescriptionStageService(GroundedDescriptionStage(discovery=NoResponse()))
    monkeypatch.setattr(
        "autoscout.weekly_workflow._make_production_enrichment",
        lambda **kwargs: service,
    )
    assert main(["generate-descriptions", "--state-dir", str(state_dir), "--output-dir", str(output_dir)]) == 2
    assert "adapter did not answer preflight" in capsys.readouterr().out
    assert state_snapshot(state_dir) == before


def _fake_success(candidate: dict[str, Any], operation_id: str, operation_date: str) -> StageArtifact:
    source = AcceptedSource(
        canonical_url=f"https://source.example.test/{candidate['candidate_id']}",
        source_kind="review",
        source_family="review",
        owner_id="owner",
        source_language="en",
        checked_date=operation_date,
        raw_title=str(candidate["title"]),
        raw_authors=("Alex Author",),
        subject_key="memory migration",
    )
    outcome = StageOutcome(
        candidate_id=str(candidate["candidate_id"]),
        identity_key=str(candidate["identity_key"]),
        outcome="single_source",
        content_description=(
            "Это русское описание книги о памяти. "
            "Второй фрагмент раскрывает документальную основу, называет проверенную "
            "страницу и уточняет редакционный ракурс книги. "
            "Третье предложение закрепляет вывод и показывает, почему книга попала в обзор."
        ),
        research_sources=(source,),
        scheduling=QueueEntry(
            candidate_id=str(candidate["candidate_id"]),
            queue_entry_operation=operation_id,
            queue_entry_date=operation_date,
            first_seen_date=operation_date,
            priority_label="D",
            attempt_status="single_source",
        ),
    )
    return StageArtifact(operation_id, operation_date, (outcome,), (outcome.scheduling,))


class InterruptingService:
    def __init__(self, interrupt_after: int | None = None) -> None:
        self.calls = 0
        self.interrupt_after = interrupt_after
        self.lock = Lock()

    def preflight(self) -> bool:
        return True

    def run(self, candidates: list[dict[str, Any]], **kwargs: Any) -> StageArtifact:
        with self.lock:
            self.calls += 1
            call = self.calls
        if self.interrupt_after is not None and call > self.interrupt_after:
            raise KeyboardInterrupt
        candidate = candidates[0]
        return _fake_success(candidate, str(kwargs["operation_id"]), str(kwargs["operation_date"]))


def test_interrupt_after_five_of_ten_persists_completed_records_and_resumes(tmp_path: Path) -> None:
    records = [
        {
            "candidate_id": f"resume-{index}",
            "identity_key": f"Resume Work {index}|Alex Author",
            "title": f"Resume Work {index}",
            "primary_authors": ["Alex Author"],
            "content_description": "",
            "short_description": f"Annotation {index}",
        }
        for index in range(10)
    ]
    state_dir, output_dir = _workspace(tmp_path, records)
    interrupted = InterruptingService(interrupt_after=5)
    with pytest.raises(KeyboardInterrupt):
        generate_descriptions(state_dir, output_dir, service=interrupted, max_workers=1, effective_date="2026-01-10")
    saved_after_interrupt = read_state(state_dir, "candidate_history")
    assert sum(record.get("grounded_outcome") == "single_source" for record in saved_after_interrupt) == 5

    resumed = InterruptingService()
    result = generate_descriptions(state_dir, output_dir, service=resumed, max_workers=1, effective_date="2026-01-10")
    assert result["eligible_count"] == 5
    assert (
        sum(record.get("grounded_outcome") == "single_source" for record in read_state(state_dir, "candidate_history"))
        == 10
    )


def test_empty_fake_stage_outcome_is_scheduled_and_valid(tmp_path: Path) -> None:
    history = [_history()[3]]
    state_dir, output_dir = _workspace(tmp_path, history)

    class EmptyStage:
        def preflight(self) -> bool:
            return True

        def run(self, candidates: list[dict[str, Any]], **kwargs: Any) -> StageArtifact:
            return StageArtifact(str(kwargs["operation_id"]), str(kwargs["operation_date"]), (), ())

    _run(state_dir, output_dir, EmptyStage())  # type: ignore[arg-type]
    artifact = read_json(output_dir / "grounded-description-stage.json")
    assert artifact["outcomes"][0]["scheduling"]["candidate_id"] == "c-blank"
    assert validate_stage_artifact(artifact)


def test_run_artifact_contains_identity_outcome_reason_and_accepted_urls(tmp_path: Path) -> None:
    history = [_history()[1]]
    state_dir, output_dir = _workspace(tmp_path, history)
    service, _ = _service()

    _run(state_dir, output_dir, service)

    artifact = read_json(output_dir / "generate-descriptions.json")
    row = artifact["touched_candidates"][0]
    assert set(row) == {"candidate_id", "identity_key", "outcome", "reason_code", "accepted_source_urls"}
    assert row["candidate_id"] == "c-codex"
    assert row["identity_key"] == "Codex Work|Alex Author"
    assert row["outcome"] == "grounded"
    assert row["reason_code"] == ""
    assert len(row["accepted_source_urls"]) == 2


@pytest.mark.parametrize("operation_date", ["2026-08-15", "2026-08-16"])
def test_generate_descriptions_renders_coherent_report_for_same_and_later_date(
    tmp_path: Path, operation_date: str
) -> None:
    history = [_history()[1]]
    state_dir, output_dir = _workspace(tmp_path, history)
    _write_coherent_report(output_dir, history[0], "2026-08-15")
    service, _ = _service()

    result = generate_descriptions(
        state_dir,
        output_dir,
        service=service,
        preflight=lambda: True,
        effective_date=operation_date,
        max_workers=1,
    )

    assert result["status"] == "ok"
    description = "Это русское описание книги Codex Work о памяти и миграции."
    assert description in (output_dir / "weekly-review.html").read_text(encoding="utf-8")
    saved = read_state(state_dir, "candidate_history")[0]
    assert saved[ATTEMPT_FIELD][-1]["date"] == operation_date


def test_generate_descriptions_rejects_incoherent_report(tmp_path: Path) -> None:
    history = [_history()[1]]
    state_dir, output_dir = _workspace(tmp_path, history)
    _write_coherent_report(output_dir, history[0], "2026-08-15")
    WeeklyReportStore(output_dir).write_json("run-summary.json", [])
    service, _ = _service()

    with pytest.raises(RuntimeError, match="review rendering failed"):
        generate_descriptions(
            state_dir,
            output_dir,
            service=service,
            preflight=lambda: True,
            effective_date="2026-08-15",
            max_workers=1,
        )


def test_standalone_production_stage_uses_its_own_budget_parameter() -> None:
    service = _make_production_enrichment(
        discovery_endpoint="https://search.example.test/api?q={query}",
        stage_budget_seconds=12345.0,
    )
    assert service.stage.stage_budget_seconds == 12345.0
