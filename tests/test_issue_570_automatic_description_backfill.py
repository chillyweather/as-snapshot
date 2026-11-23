"""Replayable acceptance tests for automatic grounded description backfill."""

from __future__ import annotations

import copy
import csv
import json
from pathlib import Path
from typing import Any

import pytest
from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import sync_state

from autoscout import grounded_description_enrichment as grounded_description_enrichment_module
from autoscout import safe_artifacts
from autoscout.content_description import DESCRIPTION_UNCONFIRMED_STATUS
from autoscout.description_enrichment import CodexDescriptionEnrichmentAgent
from autoscout.grounded_description import DISCOVERY_BUDGET_SECONDS
from autoscout.grounded_description import CodexGroundedDescriptionSynthesizer
from autoscout.grounded_description import GroundedDescriptionQueue
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import GroundedDescriptionStageService
from autoscout.grounded_description import QueueEntry
from autoscout.grounded_description import SynthesisResult
from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.review_rendering import render_review

FIXTURE = Path(__file__).parent / "fixtures" / "issue-570" / "replay.json"
FALLBACK = "Описание требует дополнительной проверки"


class ReplayDiscovery:
    def __init__(self, modes: dict[str, str] | None = None) -> None:
        self.modes = modes or {}
        self.calls: list[str] = []

    def discover(self, candidate: dict[str, Any], **_kwargs: Any) -> list[dict[str, Any]]:
        candidate_id = str(candidate["candidate_id"])
        self.calls.append(candidate_id)
        mode = self.modes.get(candidate_id)
        if mode == "transient_failure" and self.calls.count(candidate_id) == 1:
            raise RuntimeError("transient failure must not escape the stage")
        if mode in {"no_evidence", "network_failure"}:
            if mode == "network_failure":
                raise RuntimeError("network failure must not escape the stage")
            return []
        if mode == "unsupported_evidence":
            return [
                {
                    "url": f"https://unsupported.example.test/{candidate_id}",
                    "source_kind": "unclassified",
                    "source_family": "unclassified",
                    "owner_id": f"unsupported-{candidate_id}",
                    "classification": "unsupported",
                    "title": candidate["title"],
                    "primary_authors": candidate["primary_authors"],
                }
            ]
        return [
            {
                "url": f"https://publisher.example.test/{candidate_id}",
                "source_kind": "publisher",
                "source_family": "publisher_copy",
                "owner_id": f"publisher-{candidate_id}",
                "classification": "classified",
                "title": candidate["title"],
                "primary_authors": candidate["primary_authors"],
            },
            {
                "url": f"https://review.example.test/{candidate_id}",
                "source_kind": "review",
                "source_family": "review",
                "owner_id": f"review-{candidate_id}",
                "classification": "classified",
                "title": candidate["title"],
                "primary_authors": candidate["primary_authors"],
            },
        ]


class ReplayFetch:
    def fetch(self, result: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "url": result.url,
            "source_kind": result.source_kind,
            "source_family": result.source_family,
            "owner_id": result.owner_id,
            "source_language": "en",
            "title": result.title,
            "primary_authors": list(result.primary_authors),
            "subject_text": "A bounded account of memory, society, and public life.",
            "checked_date": kwargs.get("checked_date", "2026-09-14"),
            "classification": "classified",
        }


class ReplaySynthesizer:
    def synthesize(self, candidate: dict[str, Any], sources: Any) -> SynthesisResult:
        del sources
        if candidate["candidate_id"] == "candidate-copied":
            return SynthesisResult(
                "Publisher weekly product description, Publishers Weekly, Booklist, and product description: "
                "это подробное описание книги о памяти и обществе."
            )
        return SynthesisResult(
            f"Книга {candidate['title']} о памяти, обществе и общественной жизни. "
            "Второй фрагмент раскрывает документальную основу, называет проверенные "
            "страницы и уточняет редакционный ракурс книги. "
            "Третье предложение закрепляет вывод и показывает, почему книга попала в обзор."
        )


def _service(
    *, modes: dict[str, str] | None = None, stage_budget_seconds: float = 900.0
) -> tuple[GroundedDescriptionStageService, ReplayDiscovery]:
    discovery = ReplayDiscovery(modes)
    stage = GroundedDescriptionStage(
        discovery=discovery,
        fetch=ReplayFetch(),
        synthesizer=ReplaySynthesizer(),
        stage_budget_seconds=stage_budget_seconds,
        max_workers=1,
    )
    return GroundedDescriptionStageService(stage), discovery


def _workspace(tmp_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    replay = json.loads(FIXTURE.read_text(encoding="utf-8"))
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "run"
    state_dir.mkdir()
    output_dir.mkdir()
    history = copy.deepcopy(replay["candidates"])
    seed_state(state_dir, candidate_history=history)
    seed_state(state_dir, candidate_visibility=replay["visibility"])
    write_json(output_dir / "scouting-candidates.json", copy.deepcopy(replay["candidates"]))
    return state_dir, output_dir, replay


def _enrich(
    service: GroundedDescriptionStageService,
    output_dir: Path,
    state_dir: Path,
    *,
    operation_id: str = "weekly-2026-09-14",
    operation_date: str = "2026-09-14",
) -> dict[str, Any]:
    return service.enrich(output_dir, state_dir, operation_date, operation_id=operation_id)


def test_weekly_enrichment_backfills_fallback_and_refreshes_current_report(tmp_path: Path) -> None:
    state_dir, output_dir, _replay = _workspace(tmp_path)
    service, discovery = _service()

    result = _enrich(service, output_dir, state_dir)

    assert result["status"] == "ok"
    # Three fresh-work applications plus the thin backfilled record, which
    # is a depth-repair admission (issue #674), not fresh work.
    assert result["description_stage_report"]["counts"]["applied"] == 4
    assert "candidate-valid" in discovery.calls
    assert "candidate-codex-valid" in discovery.calls
    assert "candidate-editor" not in discovery.calls
    assert "candidate-hidden" not in discovery.calls
    history = read_state(state_dir, "candidate_history")
    repaired = next(record for record in history if record["candidate_id"] == "candidate-valid")
    assert repaired["content_description"].startswith("Книга Valid Work")
    assert repaired["description_provenance"]["provenance"] == "grounded"
    repair = result["description_stage_report"]["repair"]
    assert repair["backlog"] == 2
    assert repair["attempted"] == 2
    assert repair["improved"] == 2
    backfilled = next(record for record in history if record["candidate_id"] == "candidate-backfill")
    assert backfilled["content_description"].startswith("Книга Backfill Work")
    assert backfilled["description_provenance"] == {
        "identity_key": "backfill work|alex author",
        "content_description": backfilled["content_description"],
        "provenance": "grounded",
        "applied_date": "2026-09-14",
        "evidence_basis": [
            "https://review.example.test/candidate-backfill",
            "https://publisher.example.test/candidate-backfill",
        ],
        "grounding_evidence_url": "https://review.example.test/candidate-backfill",
    }
    output_history = read_json(output_dir / "candidate-history.json")
    output_backfilled = next(record for record in output_history if record["candidate_id"] == "candidate-backfill")
    assert output_backfilled == backfilled
    current = read_json(output_dir / "scouting-candidates.json")
    current_backfilled = next(record for record in current if record["candidate_id"] == "candidate-backfill")
    assert current_backfilled["description_provenance"] == backfilled["description_provenance"]
    stage = read_json(output_dir / "grounded-description-stage.json")
    assert stage["report"]["counts"]["applied"] == 4
    assert stage["report"]["budget"]["discovery_budget_seconds"] == DISCOVERY_BUDGET_SECONDS
    assert stage["report"]["count_semantics"] == {
        "classification_counts": ["applied", "skipped", "failed", "ambiguous"],
        "overlapping_final_state_counts": ["still_fallback"],
    }
    assert {record["result"] for record in stage["report"]["records"]} <= {
        "applied",
        "skipped",
        "failed",
        "ambiguous",
    }
    assert sum(
        stage["report"]["counts"][classification] for classification in ("applied", "skipped", "failed", "ambiguous")
    ) == len(stage["report"]["records"])
    applied_record = next(
        record for record in stage["report"]["records"] if record["candidate_id"] == "candidate-backfill"
    )
    assert applied_record["result"] == "applied"
    assert applied_record["evidence_urls"] == backfilled["description_provenance"]["evidence_basis"]
    assert applied_record["applied_date"] == "2026-09-14"
    applied_outcome = next(outcome for outcome in stage["outcomes"] if outcome["candidate_id"] == "candidate-backfill")
    assert applied_outcome["content_description"] == backfilled["content_description"]
    assert applied_outcome["description_provenance"] == backfilled["description_provenance"]
    assert stage["report"]["records"][0]["candidate_id"] == "candidate-backfill"


def test_description_bundle_deduplicates_same_state_and_output_path(tmp_path: Path) -> None:
    state_dir, output_dir, replay = _workspace(tmp_path)
    write_json(output_dir / "candidate-history.json", read_state(state_dir, "candidate_history"))
    write_json(output_dir / "candidate-visibility.json", replay["visibility"])
    sync_state(output_dir)
    service, _discovery = _service()

    result = service.enrich(output_dir, output_dir, replay["operation_date"], operation_id="same-directory")

    assert result["status"] == "ok"
    assert read_json(output_dir / "candidate-history.json")
    assert read_json(output_dir / "grounded-description-queue.json")["entries"]


def test_invalid_scouting_candidates_is_a_bounded_failure(tmp_path: Path) -> None:
    state_dir, output_dir, _replay = _workspace(tmp_path)
    write_json(output_dir / "scouting-candidates.json", {"not": "a candidate list"})
    service, _discovery = _service()

    result = _enrich(service, output_dir, state_dir)

    assert result["status"] == "input_failed"
    assert result["reason"] == "invalid_scouting_candidates"
    assert "not a candidate list" not in json.dumps(result)


@pytest.mark.json_backend_only("a malformed state file on disk")
def test_invalid_candidate_identity_aliases_are_a_bounded_failure(tmp_path: Path) -> None:
    state_dir, output_dir, _replay = _workspace(tmp_path)
    # An invalid alias table on disk: the State Store refuses to write it, so the file is written directly.
    write_json(state_dir / "candidate-identity-aliases.json", {"aliases": []})
    service, _discovery = _service()

    result = _enrich(service, output_dir, state_dir)

    assert result["status"] == "input_failed"
    assert result["reason"] == "invalid_candidate_identity_aliases"


def test_explicitly_disabled_stage_remains_a_quiet_skip_for_invalid_input(tmp_path: Path) -> None:
    state_dir, output_dir, _replay = _workspace(tmp_path)
    write_json(output_dir / "scouting-candidates.json", {"not": "a candidate list"})
    service, _discovery = _service()
    service.enabled = False
    service.disabled_reason = "discovery endpoint not configured"

    result = _enrich(service, output_dir, state_dir)

    assert result["status"] == "skipped"
    assert result["reason"] == "discovery endpoint not configured"


def test_production_shaped_synthesis_receives_no_more_than_candidate_envelope(tmp_path: Path) -> None:
    state_dir, output_dir, _replay = _workspace(tmp_path)
    service, _discovery = _service()
    calls: list[float] = []

    class RecordingAgent:
        def generate(
            self,
            work_items: list[dict[str, Any]],
            *,
            _prompt_override: str | None = None,
            _timeout_seconds: float | None = None,
        ) -> dict[str, Any]:
            del _prompt_override
            calls.append(float(_timeout_seconds or 0.0))
            identity_key = str(work_items[0]["identity_key"])
            return {identity_key: "Книга о памяти, обществе и общественной жизни."}

    service.stage.synthesizer = CodexGroundedDescriptionSynthesizer(agent=RecordingAgent())
    _enrich(service, output_dir, state_dir)

    assert calls
    assert 0 < calls[0] <= 90.0


def test_production_shaped_synthesis_receives_remaining_stage_budget(tmp_path: Path) -> None:
    state_dir, output_dir, _replay = _workspace(tmp_path)
    service, discovery = _service(stage_budget_seconds=100.0)
    calls: list[float] = []

    class SteppingClock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = SteppingClock()
    service.stage.monotonic = clock
    original_fetch = service.stage.fetch

    class AdvancingDiscovery:
        def discover(self, candidate: dict[str, Any], **kwargs: Any) -> Any:
            clock.value += 5.0
            return discovery.discover(candidate, **kwargs)

    class AdvancingFetch:
        def fetch(self, result: Any, **kwargs: Any) -> Any:
            clock.value += 5.0
            return original_fetch.fetch(result, **kwargs)

    service.stage.discovery = AdvancingDiscovery()
    service.stage.fetch = AdvancingFetch()

    class RecordingAgent:
        def generate(
            self,
            work_items: list[dict[str, Any]],
            *,
            _prompt_override: str | None = None,
            _timeout_seconds: float | None = None,
        ) -> dict[str, Any]:
            del _prompt_override
            calls.append(float(_timeout_seconds or 0.0))
            identity_key = str(work_items[0]["identity_key"])
            return {identity_key: "Книга о памяти, обществе и общественной жизни."}

    service.stage.synthesizer = CodexGroundedDescriptionSynthesizer(agent=RecordingAgent())
    _enrich(service, output_dir, state_dir)

    assert calls
    assert calls[0] == 80.0
    assert 0 < calls[0] < 90.0


def test_codex_agent_uses_remaining_timeout_and_skips_nonpositive_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess
    from types import SimpleNamespace

    calls: list[float] = []

    def fake_run(command: list[str], **kwargs: Any) -> Any:
        calls.append(float(kwargs["timeout"]))
        response_path = Path(command[command.index("--output-last-message") + 1])
        response_path.write_text("{}", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    agent = CodexDescriptionEnrichmentAgent(timeout_seconds=180)
    work_items = [{"identity_key": "book|author"}]

    agent.generate(work_items, _timeout_seconds=7.5)
    agent.generate(work_items, _timeout_seconds=0.0)

    assert calls == [7.5]


def test_description_bundle_failure_rolls_back_all_destinations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir, output_dir, _replay = _workspace(tmp_path)
    destinations = [
        output_dir / "grounded-description-stage.json",
        state_dir / "grounded-description-queue.json",
        state_dir / "candidate-history.json",
        output_dir / "candidate-history.json",
        output_dir / "scouting-candidates.json",
    ]
    before = {path: path.read_bytes() for path in destinations if path.exists()}
    calls = 0
    real_replace = grounded_description_enrichment_module._staged_replace

    def failing_replace(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        # The last staged replace of the bundle fails. The bundle holds four
        # files since the #688 shadow report was retired (issue #691).
        if calls == 4:
            raise OSError("must not leak this path or exception")
        real_replace(source, destination)

    monkeypatch.setattr(grounded_description_enrichment_module, "_staged_replace", failing_replace)
    service, _discovery = _service()

    result = _enrich(service, output_dir, state_dir)

    assert calls >= 4
    assert result["status"] == "persistence_failed"
    assert result["reason"] == "description_persistence_failure"
    assert "must not leak" not in json.dumps(result)
    assert {path: path.read_bytes() for path in destinations if path.exists()} == before
    assert not destinations[0].exists()
    assert not destinations[1].exists()
    assert not destinations[3].exists()
    assert not list(output_dir.glob(".*.stage"))
    assert not list(output_dir.glob(".*.backup"))
    assert not list(state_dir.glob(".*.stage"))
    assert not list(state_dir.glob(".*.backup"))


def test_description_bundle_persistent_failure_restores_every_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir, output_dir, _replay = _workspace(tmp_path)
    destinations = [
        output_dir / "grounded-description-stage.json",
        state_dir / "grounded-description-queue.json",
        state_dir / "candidate-history.json",
        output_dir / "candidate-history.json",
        output_dir / "scouting-candidates.json",
    ]
    before = {path: path.read_bytes() for path in destinations if path.exists()}
    calls = 0
    real_replace = grounded_description_enrichment_module._staged_replace

    def persistently_failing_replace(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        # From the last of the four bundle files on (issue #691).
        if calls >= 4:
            raise OSError("persistent forward publication failure")
        real_replace(source, destination)

    monkeypatch.setattr(grounded_description_enrichment_module, "_staged_replace", persistently_failing_replace)
    service, _discovery = _service()

    result = _enrich(service, output_dir, state_dir)

    assert result["status"] == "persistence_failed"
    assert calls >= 4
    assert {path: path.read_bytes() for path in destinations if path.exists()} == before
    assert not destinations[0].exists()
    assert not destinations[1].exists()
    assert not destinations[3].exists()
    assert not list(output_dir.glob(".*.stage"))
    assert not list(output_dir.glob(".*.backup"))
    assert not list(state_dir.glob(".*.stage"))
    assert not list(state_dir.glob(".*.backup"))


def test_description_bundle_recovery_failure_is_distinct_and_retains_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir, output_dir, _replay = _workspace(tmp_path)
    destinations = [
        output_dir / "grounded-description-stage.json",
        state_dir / "grounded-description-queue.json",
        state_dir / "candidate-history.json",
        output_dir / "candidate-history.json",
        output_dir / "scouting-candidates.json",
    ]
    calls = 0
    real_replace = grounded_description_enrichment_module._staged_replace
    real_recovery = safe_artifacts._trusted_recovery_replace

    def failing_forward_replace(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("recovery failure must stay bounded")
        real_replace(source, destination)

    # Canonical state travels in the State Store transaction around the bundle
    # (#1071), so the recovery that fails is the one of a run file the bundle
    # had already replaced: a stage artifact an earlier run left.
    destinations[0].write_text("{}\n", encoding="utf-8")

    def failing_recovery_replace(source: Path, destination: Path) -> None:
        if destination == destinations[0]:
            raise OSError("trusted recovery failure must stay bounded")
        real_recovery(source, destination)

    monkeypatch.setattr(grounded_description_enrichment_module, "_staged_replace", failing_forward_replace)
    monkeypatch.setattr(safe_artifacts, "_trusted_recovery_replace", failing_recovery_replace)
    service, _discovery = _service()

    result = _enrich(service, output_dir, state_dir)

    assert result["status"] == "recovery_failed"
    assert result["reason"] == "description_persistence_recovery_failure"
    assert "recovery failure must stay bounded" not in json.dumps(result)
    assert str(tmp_path) not in json.dumps(result)
    assert calls == 2
    assert any(path.name.endswith(".backup") for directory in (output_dir, state_dir) for path in directory.iterdir())


def test_valid_codex_generated_description_is_discovered_and_replaced(tmp_path: Path) -> None:
    state_dir, output_dir, replay = _workspace(tmp_path)
    original = next(item for item in replay["candidates"] if item["candidate_id"] == "candidate-codex-valid")
    service, discovery = _service()

    _enrich(service, output_dir, state_dir)

    assert "candidate-codex-valid" in discovery.calls
    record = next(
        item for item in read_state(state_dir, "candidate_history") if item["candidate_id"] == "candidate-codex-valid"
    )
    assert record["content_description"] != original["content_description"]
    assert record["description_provenance"]["provenance"] == "grounded"


@pytest.mark.parametrize("mode", ["network_failure", "no_evidence"])
def test_failed_grounding_preserves_prior_valid_codex_description(tmp_path: Path, mode: str) -> None:
    state_dir, output_dir, replay = _workspace(tmp_path)
    original = next(item for item in replay["candidates"] if item["candidate_id"] == "candidate-codex-valid")
    service, discovery = _service(modes={"candidate-codex-valid": mode})

    result = _enrich(service, output_dir, state_dir)

    assert "candidate-codex-valid" in discovery.calls
    saved = next(
        item for item in read_state(state_dir, "candidate_history") if item["candidate_id"] == "candidate-codex-valid"
    )
    assert saved["content_description"] == original["content_description"]
    assert saved["description_provenance"] == original["description_provenance"]
    assert saved["grounded_description_attempts"][-1]["reason_code"] in {"search_or_fetch_failure", "no_results"}
    assert result["description_stage_report"]["counts"]["failed"] >= 1
    assert render_review(output_dir, state_dir=state_dir, effective_date="2026-09-14") == 0
    with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
        rendered_row = next(row for row in csv.DictReader(handle) if row["Candidate ID"] == original["identity_key"])
    assert rendered_row["Description status"] == DESCRIPTION_UNCONFIRMED_STATUS
    assert rendered_row["Description"] == original["content_description"]
    assert rendered_row["Description"] != FALLBACK


def test_copied_source_prose_preserves_prior_codex_description_and_provenance(
    tmp_path: Path,
) -> None:
    state_dir, output_dir, replay = _workspace(tmp_path)
    original = next(item for item in replay["candidates"] if item["candidate_id"] == "candidate-codex-valid")
    prior_description = original["content_description"]
    prior_provenance = copy.deepcopy(original["description_provenance"])
    copied = "Издатель подробно рассказывает о памяти, обществе и общественной жизни книги."
    service, _discovery = _service()
    base_fetch = ReplayFetch()
    base_synthesizer = ReplaySynthesizer()

    class CopiedSourceFetch:
        def fetch(self, result: Any, **kwargs: Any) -> dict[str, Any]:
            fetched = base_fetch.fetch(result, **kwargs)
            fetched["source_language"] = "ru"
            fetched["subject_text"] = copied
            return fetched

    class CopiedSourceSynthesizer:
        def synthesize(self, candidate: dict[str, Any], sources: Any) -> SynthesisResult:
            if candidate["candidate_id"] == "candidate-codex-valid":
                return SynthesisResult(copied)
            return base_synthesizer.synthesize(candidate, sources)

    service.stage.fetch = CopiedSourceFetch()
    service.stage.synthesizer = CopiedSourceSynthesizer()

    _enrich(service, output_dir, state_dir)

    saved = next(
        item for item in read_state(state_dir, "candidate_history") if item["candidate_id"] == original["candidate_id"]
    )
    assert saved["content_description"] == prior_description
    assert saved["description_provenance"] == prior_provenance
    attempt = saved["grounded_description_attempts"][-1]
    assert attempt["outcome"] == "attempted_no_description"
    assert attempt["reason_code"] == "invalid_description"
    stage = read_json(output_dir / "grounded-description-stage.json")
    serialized = json.dumps(stage, ensure_ascii=False)
    assert copied not in serialized
    assert "rejected as copied publisher/review prose" in serialized


def test_exact_copied_russian_source_prose_is_rejected(tmp_path: Path) -> None:
    state_dir, output_dir, _replay = _workspace(tmp_path)
    copied = "Издатель рассказывает о памяти, обществе и общественной жизни книги."
    service, _discovery = _service()

    class ExactSourceFetch:
        def fetch(self, result: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "url": result.url,
                "source_kind": result.source_kind,
                "source_family": result.source_family,
                "owner_id": result.owner_id,
                "source_language": "ru",
                "title": result.title,
                "primary_authors": list(result.primary_authors),
                "subject_text": copied,
                "checked_date": kwargs.get("checked_date", "2026-09-14"),
                "classification": "classified",
            }

    class ExactCopiedSynthesizer:
        def synthesize(self, candidate: dict[str, Any], sources: Any) -> SynthesisResult:
            del candidate, sources
            return SynthesisResult(copied)

    service.stage.fetch = ExactSourceFetch()
    service.stage.synthesizer = ExactCopiedSynthesizer()

    result = _enrich(service, output_dir, state_dir)

    record = next(
        item for item in result["description_stage_report"]["records"] if item["candidate_id"] == "candidate-backfill"
    )
    assert record["result"] == "failed"
    assert record["reason_code"] == "invalid_description"
    stage = read_json(output_dir / "grounded-description-stage.json")
    outcome = next(item for item in stage["outcomes"] if item["candidate_id"] == "candidate-backfill")
    assert outcome["reason_code"] == "invalid_description"
    assert outcome["reason_detail"] == "rejected as copied publisher/review prose"
    assert copied not in json.dumps(outcome, ensure_ascii=False)
    history = read_state(state_dir, "candidate_history")
    saved = next(item for item in history if item["candidate_id"] == "candidate-backfill")
    assert saved["content_description"] == FALLBACK


def _run_prose_case(tmp_path: Path, source_text: str, generated_text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    state_dir, output_dir, _replay = _workspace(tmp_path)
    service, _discovery = _service()

    class SourceFetch:
        def fetch(self, result: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "url": result.url,
                "source_kind": result.source_kind,
                "source_family": result.source_family,
                "owner_id": result.owner_id,
                "source_language": "ru",
                "title": result.title,
                "primary_authors": list(result.primary_authors),
                "subject_text": source_text,
                "checked_date": kwargs.get("checked_date", "2026-09-14"),
                "classification": "classified",
            }

    class CaseSynthesizer:
        def synthesize(self, candidate: dict[str, Any], sources: Any) -> SynthesisResult:
            del sources
            if candidate["candidate_id"] == "candidate-backfill":
                return SynthesisResult(generated_text)
            return SynthesisResult(
                f"Книга {candidate['title']} о памяти и общественной жизни. "
                "Второй фрагмент раскрывает документальную основу, называет проверенные "
                "страницы и уточняет редакционный ракурс книги. "
                "Третье предложение закрепляет вывод и показывает, почему книга попала в обзор."
            )

    service.stage.fetch = SourceFetch()
    service.stage.synthesizer = CaseSynthesizer()
    result = _enrich(service, output_dir, state_dir)
    stage = read_json(output_dir / "grounded-description-stage.json")
    outcome = next(item for item in stage["outcomes"] if item["candidate_id"] == "candidate-backfill")
    return result, outcome


@pytest.mark.parametrize(
    ("source_text", "generated_text"),
    [
        (
            "Издатель рассказывает о памяти, обществе и общественной жизни книги.",
            "  издатель   рассказывает о памяти, обществе и общественной жизни книги. ",
        ),
        (
            "Издатель подробно рассказывает о памяти, обществе и общественной жизни книги.",
            "Оригинальная первая фраза. Издатель подробно рассказывает о памяти, обществе и общественной жизни.",
        ),
    ],
    ids=["whitespace-and-case-normalized-copy", "substantial-contiguous-copy"],
)
def test_copied_source_prose_is_rejected_without_leaking_text(
    tmp_path: Path, source_text: str, generated_text: str
) -> None:
    result, outcome = _run_prose_case(tmp_path, source_text, generated_text)

    assert outcome["outcome"] == "attempted_no_description"
    assert outcome["reason_code"] == "invalid_description"
    assert "Издатель" not in outcome.get("reason_detail", "")
    report_record = next(
        item for item in result["description_stage_report"]["records"] if item["candidate_id"] == "candidate-backfill"
    )
    assert report_record["result"] == "failed"


def test_original_synthesis_with_short_factual_overlap_is_applied(tmp_path: Path) -> None:
    result, outcome = _run_prose_case(
        tmp_path,
        "Книга о памяти и обществе в современном мире.",
        "Это оригинальный рассказ о памяти и обществе, который связывает личный опыт с общественной жизнью. "
        "Второй фрагмент раскрывает документальную основу и уточняет редакционный ракурс книги. "
        "Третье предложение закрепляет вывод и показывает, почему книга попала в обзор.",
    )

    assert outcome["outcome"] == "grounded"
    assert outcome["reason_code"] == ""
    record = next(
        item for item in result["description_stage_report"]["records"] if item["candidate_id"] == "candidate-backfill"
    )
    assert record["result"] == "applied"


def test_invalid_generated_prose_preserves_prior_valid_codex_description(tmp_path: Path) -> None:
    state_dir, output_dir, replay = _workspace(tmp_path)
    original = next(item for item in replay["candidates"] if item["candidate_id"] == "candidate-codex-valid")
    service, _discovery = _service()

    class InvalidSynthesizer:
        def synthesize(self, candidate: dict[str, Any], sources: Any) -> SynthesisResult:
            del candidate, sources
            return SynthesisResult("This generated prose is not valid Russian.")

    service.stage.synthesizer = InvalidSynthesizer()
    _enrich(service, output_dir, state_dir)

    saved = next(
        item for item in read_state(state_dir, "candidate_history") if item["candidate_id"] == "candidate-codex-valid"
    )
    assert saved["content_description"] == original["content_description"]
    assert saved["description_provenance"] == original["description_provenance"]
    assert saved["grounded_description_attempts"][-1]["reason_code"] == "invalid_description"


def test_ambiguous_identity_preserves_prior_valid_codex_description(tmp_path: Path) -> None:
    state_dir, output_dir, replay = _workspace(tmp_path)
    original = next(item for item in replay["candidates"] if item["candidate_id"] == "candidate-codex-valid")
    ambiguous = {
        **original,
        "candidate_id": "candidate-codex-ambiguous",
        "identity_key": "ambiguous current|alex author",
    }
    history = read_state(state_dir, "candidate_history")
    history.extend(
        [
            {**ambiguous, "identity_key": "ambiguous one|alex author"},
            {**ambiguous, "identity_key": "ambiguous two|alex author"},
        ]
    )
    seed_state(state_dir, candidate_history=history)
    write_json(output_dir / "scouting-candidates.json", [ambiguous])
    service, _discovery = _service()

    result = _enrich(service, output_dir, state_dir)

    saved = [
        item
        for item in read_state(state_dir, "candidate_history")
        if item["candidate_id"] == "candidate-codex-ambiguous"
    ]
    assert saved
    assert all(item["content_description"] == original["content_description"] for item in saved)
    assert all(item["description_provenance"] == original["description_provenance"] for item in saved)
    assert result["description_stage_report"]["counts"]["ambiguous"] == 1


def test_ambiguous_current_identity_does_not_copy_duplicate_history_description(
    tmp_path: Path,
) -> None:
    state_dir, output_dir, replay = _workspace(tmp_path)
    original = next(item for item in replay["candidates"] if item["candidate_id"] == "candidate-codex-valid")
    current = {
        **original,
        "candidate_id": "candidate-codex-ambiguous-current",
        "identity_key": "ambiguous current|alex author",
        "content_description": "Исходное описание текущей записи.",
        "description_provenance": {
            "identity_key": "ambiguous current|alex author",
            "content_description": "Исходное описание текущей записи.",
            "provenance": "codex-generated",
            "applied_date": "2026-09-14",
        },
    }
    first_duplicate = {
        **current,
        "identity_key": "ambiguous one|alex author",
        "content_description": "Первое описание дубликата.",
        "description_provenance": {
            "identity_key": "ambiguous one|alex author",
            "content_description": "Первое описание дубликата.",
            "provenance": "codex-generated",
            "applied_date": "2026-09-13",
        },
    }
    second_duplicate = {
        **current,
        "identity_key": "ambiguous two|alex author",
        "content_description": "Второе описание дубликата.",
        "description_provenance": {
            "identity_key": "ambiguous two|alex author",
            "content_description": "Второе описание дубликата.",
            "provenance": "codex-generated",
            "applied_date": "2026-09-12",
        },
    }
    history = read_state(state_dir, "candidate_history")
    history.extend([first_duplicate, second_duplicate])
    seed_state(state_dir, candidate_history=history)
    write_json(output_dir / "scouting-candidates.json", [current])
    service, _discovery = _service()

    result = _enrich(service, output_dir, state_dir)

    assert result["description_stage_report"]["counts"]["ambiguous"] == 1
    refreshed = read_json(output_dir / "scouting-candidates.json")[0]
    assert refreshed["content_description"] == current["content_description"]
    assert refreshed["description_provenance"] == current["description_provenance"]
    assert refreshed["content_description"] not in {
        first_duplicate["content_description"],
        second_duplicate["content_description"],
    }
    assert refreshed["description_provenance"] not in (
        first_duplicate["description_provenance"],
        second_duplicate["description_provenance"],
    )


def test_budget_non_admission_preserves_prior_valid_codex_description(tmp_path: Path) -> None:
    state_dir, output_dir, replay = _workspace(tmp_path)
    original = next(item for item in replay["candidates"] if item["candidate_id"] == "candidate-codex-valid")
    service, discovery = _service(stage_budget_seconds=0.0)

    _enrich(service, output_dir, state_dir)

    assert "candidate-codex-valid" not in discovery.calls
    saved = next(
        item for item in read_state(state_dir, "candidate_history") if item["candidate_id"] == "candidate-codex-valid"
    )
    assert saved["content_description"] == original["content_description"]
    assert saved["description_provenance"] == original["description_provenance"]
    assert saved["grounded_description_attempts"][-1]["reason_code"] == "not_attempted_stage_budget"


def test_sufficient_and_editor_corrected_records_are_not_discovered(tmp_path: Path) -> None:
    """Sufficient descriptions and editor-corrected records are never discovered.

    The shared replay fixture keeps ``candidate-valid`` thin on purpose, so
    other tests can prove thin backfilled records enter repair. Here the
    valid record carries depth-qualifying text instead, which keeps this
    test proving its original point: sufficient records are never
    discovered, and editor-corrected records are never touched.
    """
    state_dir, output_dir, replay = _workspace(tmp_path)
    sufficient_text = (
        "Уже проверенное подробное описание книги о памяти и обществе. "
        "Второй фрагмент раскрывает документальную основу и уточняет редакционный ракурс. "
        "Третье предложение закрепляет вывод и показывает, почему книга попала в обзор."
    )
    history = read_state(state_dir, "candidate_history")
    for record in history:
        if record["candidate_id"] == "candidate-valid":
            record["content_description"] = sufficient_text
            record["description_provenance"] = {
                **record["description_provenance"],
                "content_description": sufficient_text,
            }
    seed_state(state_dir, candidate_history=history)
    before = {
        record["candidate_id"]: (
            record["content_description"],
            copy.deepcopy(record["description_provenance"]),
        )
        for record in history
        if record["candidate_id"] in {"candidate-valid", "candidate-editor"}
    }
    service, discovery = _service()

    _enrich(service, output_dir, state_dir)

    assert "candidate-valid" not in discovery.calls
    assert "candidate-editor" not in discovery.calls
    after = read_state(state_dir, "candidate_history")
    for candidate_id, expected in before.items():
        record = next(item for item in after if item["candidate_id"] == candidate_id)
        assert (record["content_description"], record["description_provenance"]) == expected


def test_fail_closed_results_have_bounded_reason_records_and_no_source_prose(tmp_path: Path) -> None:
    state_dir, output_dir, _replay = _workspace(tmp_path)
    service, _discovery = _service(
        modes={
            "candidate-invalid": "unsupported_evidence",
            "candidate-copied": "good",
        }
    )

    result = _enrich(service, output_dir, state_dir)
    report = result["description_stage_report"]
    records = {record["candidate_id"]: record for record in report["records"]}

    assert records["candidate-invalid"]["result"] == "failed"
    assert records["candidate-invalid"]["final_state"] == "still_fallback"
    assert records["candidate-invalid"]["reason_code"] == "source_denylisted"
    assert records["candidate-copied"]["result"] == "failed"
    assert records["candidate-copied"]["final_state"] == "still_fallback"
    assert records["candidate-copied"]["reason_code"] == "invalid_description"
    assert report["counts"]["still_fallback"] == 2
    assert report["counts"]["failed"] == 2
    stage = read_json(output_dir / "grounded-description-stage.json")
    outcomes = {outcome["candidate_id"]: outcome for outcome in stage["outcomes"]}
    assert "rejected as copied publisher/review prose" in outcomes["candidate-copied"]["reason_detail"]
    serialized = json.dumps(stage, ensure_ascii=False)
    assert "publisher weekly product description" not in serialized
    history = read_state(state_dir, "candidate_history")
    for candidate_id in ("candidate-invalid", "candidate-copied"):
        record = next(item for item in history if item["candidate_id"] == candidate_id)
        assert record["content_description"] == FALLBACK


def test_ambiguous_identity_and_whole_stage_failure_leave_fallback(tmp_path: Path) -> None:
    state_dir, output_dir, replay = _workspace(tmp_path)
    ambiguous = replay["candidates"][0].copy()
    ambiguous["candidate_id"] = "candidate-ambiguous"
    ambiguous["identity_key"] = "ambiguous current|alex author"
    replay_history = read_state(state_dir, "candidate_history")
    replay_history.extend(
        [
            {
                **ambiguous,
                "identity_key": "ambiguous one|alex author",
            },
            {
                **ambiguous,
                "identity_key": "ambiguous two|alex author",
            },
        ]
    )
    seed_state(state_dir, candidate_history=replay_history)
    write_json(output_dir / "scouting-candidates.json", [ambiguous])
    service, _discovery = _service()
    first = _enrich(service, output_dir, state_dir)
    ambiguous_record = next(
        record
        for record in first["description_stage_report"]["records"]
        if record["candidate_id"] == "candidate-ambiguous"
    )
    assert ambiguous_record["result"] == "ambiguous"
    assert ambiguous_record["reason_code"] == "ambiguity"
    before_ambiguous = {
        record["identity_key"]: (
            record.get("content_description"),
            copy.deepcopy(record.get("grounded_description_attempts", [])),
        )
        for record in read_state(state_dir, "candidate_history")
        if record.get("candidate_id") == "candidate-ambiguous"
    }
    assert before_ambiguous
    after_ambiguous = {
        record["identity_key"]: (
            record.get("content_description"),
            record.get("grounded_description_attempts", []),
        )
        for record in read_state(state_dir, "candidate_history")
        if record.get("candidate_id") == "candidate-ambiguous"
    }
    assert after_ambiguous == before_ambiguous

    class BrokenStage:
        def run(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("must not leak")

    broken_service = GroundedDescriptionStageService(BrokenStage())  # type: ignore[arg-type]
    before_failure = read_state(state_dir, "candidate_history")
    second = _enrich(broken_service, output_dir, state_dir, operation_id="weekly-broken")
    assert second["status"] == "failed"
    failed_records = [
        record for record in second["description_stage_report"]["records"] if record["result"] == "failed"
    ]
    assert second["description_stage_report"]["counts"]["failed"] == len(failed_records) == 2
    assert second["description_stage_report"]["counts"]["still_fallback"] == 1
    assert {record["candidate_id"] for record in failed_records} == {
        "candidate-ambiguous",
        "candidate-copied",
    }
    assert all(record["reason_code"] == "stage_execution_failure" for record in failed_records)
    after_failure = read_state(state_dir, "candidate_history")
    assert after_failure == before_failure
    queue = read_json(state_dir / "grounded-description-queue.json")
    queued = next(item for item in queue["entries"] if item["candidate_id"] == "candidate-ambiguous")
    assert queued["attempt_status"] == "waiting"
    assert "must not leak" not in json.dumps(read_json(output_dir / "grounded-description-stage.json"))


def test_same_operation_transient_failure_retries_and_upserts_one_attempt(tmp_path: Path) -> None:
    state_dir, output_dir, _replay = _workspace(tmp_path)
    service, discovery = _service(modes={"candidate-backfill": "transient_failure"})

    first = _enrich(service, output_dir, state_dir)
    first_record = next(
        item for item in read_state(state_dir, "candidate_history") if item["candidate_id"] == "candidate-backfill"
    )
    assert first["description_stage_report"]["records"]
    assert first_record["grounded_description_attempts"][0]["reason_code"] == "search_or_fetch_failure"

    second = _enrich(service, output_dir, state_dir)
    second_record = next(
        item for item in read_state(state_dir, "candidate_history") if item["candidate_id"] == "candidate-backfill"
    )
    assert discovery.calls.count("candidate-backfill") == 2
    assert second["description_stage_report"]["counts"]["applied"] == 1
    assert second_record["content_description"].startswith("Книга Backfill Work")
    assert len(second_record["grounded_description_attempts"]) == 1
    assert second_record["grounded_description_attempts"][0]["outcome"] == "grounded"


def test_whole_stage_failure_same_operation_retry_recovers(tmp_path: Path) -> None:
    state_dir, output_dir, replay = _workspace(tmp_path)
    candidate = next(item for item in replay["candidates"] if item["candidate_id"] == "candidate-backfill")
    seed_state(state_dir, candidate_history=[copy.deepcopy(candidate)])
    seed_state(state_dir, candidate_visibility=[])
    write_json(output_dir / "scouting-candidates.json", [copy.deepcopy(candidate)])

    class BrokenStage:
        def run(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("whole stage failed")

    broken = GroundedDescriptionStageService(BrokenStage())  # type: ignore[arg-type]
    first = _enrich(broken, output_dir, state_dir, operation_id="weekly-retry")
    assert first["status"] == "failed"
    before = read_state(state_dir, "candidate_history")
    assert "grounded_description_attempts" not in before[0]

    working, _discovery = _service()
    second = _enrich(working, output_dir, state_dir, operation_id="weekly-retry")
    after = read_state(state_dir, "candidate_history")[0]
    assert second["description_stage_report"]["counts"]["applied"] == 1
    assert after["content_description"].startswith("Книга Backfill Work")
    assert len(after["grounded_description_attempts"]) == 1


def test_legacy_same_date_attempt_does_not_block_new_operation(tmp_path: Path) -> None:
    state_dir, output_dir, _replay = _workspace(tmp_path)
    history = read_state(state_dir, "candidate_history")
    record = next(item for item in history if item["candidate_id"] == "candidate-backfill")
    record["grounded_description_attempts"] = [
        {"date": "2026-09-14", "outcome": "attempted_no_description", "reason_code": "no_results"}
    ]
    seed_state(state_dir, candidate_history=history)

    service, _discovery = _service()
    result = _enrich(service, output_dir, state_dir, operation_id="weekly-new")

    saved = next(
        item for item in read_state(state_dir, "candidate_history") if item["candidate_id"] == "candidate-backfill"
    )
    # Three fresh-work applications plus the thin backfilled record's repair (issue #674).
    assert result["description_stage_report"]["counts"]["applied"] == 4
    assert len(saved["grounded_description_attempts"]) == 2
    assert saved["grounded_description_attempts"][0]["date"] == "2026-09-14"
    assert saved["grounded_description_attempts"][1]["operation_id"] == "weekly-new"


def test_same_operation_is_idempotent_and_later_operation_can_retry(tmp_path: Path) -> None:
    state_dir, output_dir, _replay = _workspace(tmp_path)
    service, discovery = _service(modes={"candidate-backfill": "no_evidence"})

    first = _enrich(service, output_dir, state_dir)
    first_history = read_state(state_dir, "candidate_history")
    first_attempts = next(item for item in first_history if item["candidate_id"] == "candidate-backfill").get(
        "grounded_description_attempts", []
    )
    second = _enrich(service, output_dir, state_dir)
    second_history = read_state(state_dir, "candidate_history")
    second_attempts = next(item for item in second_history if item["candidate_id"] == "candidate-backfill").get(
        "grounded_description_attempts", []
    )
    assert len(first_attempts) == len(second_attempts) == 1
    successful_candidate = next(item for item in second_history if item["candidate_id"] == "candidate-invalid")
    assert len(successful_candidate["grounded_description_attempts"]) == 1
    assert first["description_stage_report"]["counts"]["failed"] == 2
    assert second["description_stage_report"]["counts"]["skipped"] >= 1

    service, discovery = _service()
    third = _enrich(service, output_dir, state_dir, operation_id="weekly-2026-09-21", operation_date="2026-09-21")
    assert "candidate-backfill" in discovery.calls
    assert third["description_stage_report"]["counts"]["applied"] == 1
    retried = next(
        item for item in read_state(state_dir, "candidate_history") if item["candidate_id"] == "candidate-backfill"
    )
    assert len(retried["grounded_description_attempts"]) == 2


def test_eligible_queue_preserves_age_when_budget_does_not_admit_candidates() -> None:
    older = QueueEntry(
        candidate_id="candidate-old",
        queue_entry_operation="operation-2026-09-01",
        queue_entry_date="2026-09-01",
        first_seen_date="2026-09-01",
        priority_label="D",
    )
    newer = QueueEntry(
        candidate_id="candidate-new",
        queue_entry_operation="operation-2026-09-13",
        queue_entry_date="2026-09-13",
        first_seen_date="2026-09-13",
        priority_label="A",
    )
    queue = GroundedDescriptionQueue({"candidate-old": older, "candidate-new": newer})
    candidates = [
        {"candidate_id": "candidate-new", "priority_label": "A", "first_seen_date": "2026-09-13"},
        {"candidate_id": "candidate-old", "priority_label": "D", "first_seen_date": "2026-09-01"},
    ]

    ordered = queue.worklist(
        candidates,
        operation_id="operation-current",
        operation_date="2026-09-14",
        eligible_candidate_ids={"candidate-old", "candidate-new"},
    )

    # Issue #634 orders the work list by priority label before first-seen
    # date, so the newer A-label candidate runs before the older D-label one.
    # Queue age is still preserved on the entries themselves below.
    assert [candidate["candidate_id"] for candidate, _entry in ordered] == ["candidate-new", "candidate-old"]
    assert queue.entries["candidate-old"].queue_entry_operation == "operation-2026-09-01"
    assert queue.entries["candidate-old"].queue_entry_date == "2026-09-01"
    assert queue.entries["candidate-new"].queue_entry_operation == "operation-2026-09-13"
    assert queue.entries["candidate-new"].queue_entry_date == "2026-09-13"


def test_budget_non_admission_is_reported_and_remains_queued(tmp_path: Path) -> None:
    state_dir, output_dir, _replay = _workspace(tmp_path)
    service, discovery = _service(stage_budget_seconds=0.0)

    result = _enrich(service, output_dir, state_dir)

    assert discovery.calls == []
    assert result["description_stage_report"]["counts"]["still_fallback"] >= 1
    assert result["description_stage_report"]["attention_required"] is False
    record = next(
        item for item in result["description_stage_report"]["records"] if item["candidate_id"] == "candidate-backfill"
    )
    assert record["reason_code"] == "not_attempted_stage_budget"
    queue = read_json(state_dir / "grounded-description-queue.json")
    queued = next(item for item in queue["entries"] if item["candidate_id"] == "candidate-backfill")
    assert queued["attempt_status"] == "waiting"
