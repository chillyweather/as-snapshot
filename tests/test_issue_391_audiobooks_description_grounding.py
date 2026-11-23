"""Tests for Issue #391: grounded Audiobooks.com description enrichment.

The Audiobooks.com retailer category is not authoritative for the underlying
work. Before this issue, an enrichment agent could accept a plausible-looking
Russian description built from the title alone, keep the retailer category,
and present fiction (e.g. Kindred by Octavia E. Butler, Ghost Girl by Eva
Benefield) as adult nonfiction scouting candidates.

These tests pin the bounded, source-aware grounding contract at the public
``run_weekly_workflow`` seam:

* Audiobooks.com work items carry a grounding contract with exact allowed
  Evidence URLs and metadata-only replay entries (no copied prose).
* A non-fallback Content Description is accepted only with structured
  authoritative evidence tied to an allowed existing Evidence URL, an
  established subject, and closed work-type and genre values.
* Reliable Kindred evidence produces an original Russian description whose
  provenance keeps the supporting Evidence URL; the authoritative genre
  replaces the retailer category and an authoritative pure-fiction result
  passes through the canonical exclusion rule into an internal exclusion
  record.
* Unavailable Ghost Girl evidence produces the standard fallback with an
  explicit bounded reason persisted in candidate state and description
  coverage.
* Invalid grounded prose - English-only or copied text, or the literal
  standard fallback string - is rejected by the grounding gate with a
  bounded reason before any authoritative metadata can be applied.
* The free-form subject statement is acceptance-gate evidence only and
  never enters candidate or editor-facing state.
* Existing non-Audiobooks description behavior is unchanged.
* All replay is deterministic and local: no live network or model call.
"""

from __future__ import annotations

import csv
import json
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from state_store_support import read_state

from autoscout.content_description import DESCRIPTION_FALLBACK
from autoscout.content_description import apply_description_artifacts
from autoscout.content_description import is_fiction_genre_category
from autoscout.content_description import normalized_genre_category
from autoscout.content_description import validate_content_description
from autoscout.description_enrichment import GROUNDING_REASON_AGENT_FAILURE
from autoscout.description_enrichment import GROUNDING_REASON_INVALID_PROSE
from autoscout.description_enrichment import GROUNDING_REASON_MISSING_EVIDENCE
from autoscout.description_enrichment import GROUNDING_REASON_NO_DESCRIPTION
from autoscout.description_enrichment import GROUNDING_REASON_UNAVAILABLE
from autoscout.description_enrichment import GROUNDING_REASON_UNSUPPORTED_URL
from autoscout.scouting_candidates import ScoutingCandidate
from autoscout.source_collectors import FORBIDDEN_COLLECTOR_RESPONSE_FIELDS
from autoscout.weekly_workflow import WorkflowOutcome
from autoscout.weekly_workflow import deterministic_adapters
from autoscout.weekly_workflow import run_weekly_workflow

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "issue-391-audiobooks-description-grounding"

AUDIOBOOKS_NAME = "Audiobooks.com Nonfiction Audiobook Bestsellers"
AUDIOBOOKS_URL = "https://www.audiobooks.com/browse/non-fiction"
KINDRED_URL = "https://www.audiobooks.com/audiobook/kindred/283469"
GHOST_URL = "https://www.audiobooks.com/audiobook/ghost-girl/981321"
KINDRED_ID = "kindred|octavia e butler"
GHOST_ID = "ghost girl|eva benefield"
TEST_BOOK_ID = "test book|test author"

TEST_BOOK_DESCRIPTION = "Описание книги «Test Book» на русском языке."


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ── Workflow setup helpers ───────────────────────────────────────────────


def _write_config(tmp_path: Path, *, search_optimization_trial: bool = False) -> Path:
    config_path = tmp_path / "config.json"
    _write_json(
        config_path,
        {
            "run_date": "2026-08-05",
            "readiness_purpose": "recurring-weekly",
            "editor_row_target": 100,
            "pilot_row_min": 100,
            "pilot_row_max": 100,
            "pilot_mode": True,
            "search_optimization_trial": search_optimization_trial,
            "sources": [
                {
                    "name": AUDIOBOOKS_NAME,
                    "family": "audiobook ranking",
                    "url": AUDIOBOOKS_URL,
                    "access_status": "open",
                    "check_frequency": "weekly",
                    "language": "en",
                    "extraction_method": "codex-extraction-replay",
                    "expected_page_heading": "Non-Fiction",
                    "expected_sort_label": "Sort by: Bestselling",
                    "source_page_fixture_file": str(FIXTURES / "audiobooks-capture.json"),
                    "extraction_response_file": "audiobooks-extraction.json",
                },
                {
                    "name": "Test Source",
                    "family": "award",
                    "url": "https://example.test/source",
                    "access_status": "open",
                    "extraction_method": "codex-extraction-replay",
                    "check_frequency": "weekly",
                    "language": "en",
                    "source_page_fixture_file": "test-capture.json",
                    "extraction_response_file": "test-source-codex-extraction.json",
                },
            ],
        },
    )
    return config_path


def _write_extraction_files(output_dir: Path) -> None:
    """Write the per-source extraction artifacts named by the worklist.

    The envelope echoes the worklist's operation identity so the artifact
    binds as fresh evidence (same pattern as test_issue_272).
    """
    worklist = _read_json(output_dir / "codex-extraction-worklist.json")
    entries_by_source = {
        AUDIOBOOKS_NAME: _read_json(FIXTURES / "audiobooks-extraction.json")["mentions"],
        "Test Source": [
            {
                "title": "Test Book",
                "primary_authors": ["Test Author"],
                "source_url": "https://example.test/book",
                "evidence_url": "https://example.test/book",
                "evidence_note": "Listed book.",
                "candidate_category": "popular science",
                "signal_type": "award shortlist",
                "signal_strength": "shortlist",
            }
        ],
    }
    for item in worklist["work_items"]:
        source_name = str(item["source_name"])
        _write_json(
            output_dir / str(item["expected_extraction_file"]),
            {
                "source_name": source_name,
                "source_url": str(item.get("source_url", "")),
                "operation_id": str(item.get("operation_id", "")),
                "capture_sha256": str(item.get("capture_sha256", "")),
                "entries": entries_by_source[source_name],
            },
        )


def _setup_run(tmp_path: Path, *, search_optimization_trial: bool = False) -> tuple[Path, Path]:
    config_path = _write_config(tmp_path, search_optimization_trial=search_optimization_trial)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    (tmp_path / "test-capture.json").write_text(
        json.dumps(
            {
                "source_url": "https://example.test/source",
                "html": '<html><body><a href="https://example.test/book">Link</a> by Test Author</body></html>',
            }
        ),
        encoding="utf-8",
    )
    adapters = deterministic_adapters()
    adapters.capture.prepare(config_path, output_dir, live=False)
    _write_extraction_files(output_dir)
    return config_path, output_dir


def _make_adapters(agent: Any) -> Any:
    from autoscout.description_enrichment import DescriptionEnrichmentService

    adapters = deterministic_adapters()
    adapters.enrichment = DescriptionEnrichmentService(agent=agent)
    # The description-adjudication stage stays wired here on purpose. These
    # tests pin the grounded reason the editor reads, and the stage runs
    # over the same fallback records every week, so this suite is what
    # proves the stage does not take that reason away (issue #640).
    return adapters


def _load_pages() -> dict[str, dict[str, Any]]:
    return {
        KINDRED_URL: _read_json(FIXTURES / "kindred-authoritative-page.json"),
        GHOST_URL: _read_json(FIXTURES / "ghost-girl-authoritative-page.json"),
    }


# ── Replay agents ────────────────────────────────────────────────────────


class ReplayDescriptionAgent:
    """Deterministic replay of the grounded Audiobooks.com description agent.

    Reads only the offline authoritative-page fixtures: reliable Kindred
    evidence yields an original Russian description, unavailable Ghost Girl
    evidence reports ``evidence_available: false``. Non-Audiobooks items get
    a plain Russian string, the unchanged legacy contract.
    """

    def __init__(self, pages: dict[str, dict[str, Any]]) -> None:
        self._pages = pages
        self.calls: list[list[dict[str, Any]]] = []

    @staticmethod
    def _evidence_urls(work_item: dict[str, Any]) -> list[str]:
        urls: list[str] = []
        for evidence in work_item.get("metadata", {}).get("source_evidence", []):
            if not isinstance(evidence, dict):
                continue
            url = str(evidence.get("evidence_url") or evidence.get("source_url", "")).strip()
            if url:
                urls.append(url)
        return urls

    def generate(self, work_items: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls.append(list(work_items))
        result: dict[str, Any] = {}
        for work_item in work_items:
            identity_key = str(work_item.get("identity_key", ""))
            urls = self._evidence_urls(work_item)
            is_audiobooks = any(
                "audiobooks.com" in str(evidence.get("source_name", "")).casefold()
                for evidence in work_item.get("metadata", {}).get("source_evidence", [])
            )
            if is_audiobooks:
                page = next((self._pages[url] for url in urls if url in self._pages), None)
                if page is None or not page.get("available"):
                    result[identity_key] = {
                        "content_description": "",
                        "grounding_evidence": {"evidence_url": urls[0], "evidence_available": False},
                    }
                else:
                    result[identity_key] = {
                        "content_description": page["content_description"],
                        "grounding_evidence": {
                            "evidence_url": page["evidence_url"],
                            "evidence_available": True,
                            "subject": page["subject"],
                            "work_type": page["work_type"],
                            "genre": page["genre"],
                        },
                    }
            else:
                result[identity_key] = f"Описание книги «{work_item.get('title', '')}» на русском языке."
        return result


class TitleOnlyAgent:
    """An agent that writes a description from the title alone."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    def generate(self, work_items: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls.append(list(work_items))
        return {
            str(work_item.get("identity_key", "")): f"Название задаёт тематический фокус: {work_item.get('title', '')}."
            for work_item in work_items
        }


class WrongUrlAgent:
    """An agent whose grounding evidence points outside the allowed URLs."""

    def generate(self, work_items: list[dict[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for work_item in work_items:
            identity_key = str(work_item.get("identity_key", ""))
            if identity_key == KINDRED_ID:
                result[identity_key] = {
                    "content_description": "Роман о наследии рабства и семейной памяти.",
                    "grounding_evidence": {
                        "evidence_url": "https://www.goodreads.example/book/kindred",
                        "evidence_available": True,
                        "subject": "Kindred novel.",
                        "work_type": "fiction",
                        "genre": "Science Fiction",
                    },
                }
            else:
                result[identity_key] = f"Описание книги «{work_item.get('title', '')}» на русском языке."
        return result


class EmptyDescriptionAgent:
    """An agent whose grounding evidence is valid but whose description is empty."""

    def generate(self, work_items: list[dict[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for work_item in work_items:
            identity_key = str(work_item.get("identity_key", ""))
            if identity_key == KINDRED_ID:
                result[identity_key] = {
                    "content_description": "",
                    "grounding_evidence": {
                        "evidence_url": KINDRED_URL,
                        "evidence_available": True,
                        "subject": "Science-fiction novel about the legacy of slavery.",
                        "work_type": "fiction",
                        "genre": "Science Fiction",
                    },
                }
            else:
                result[identity_key] = f"Описание книги «{work_item.get('title', '')}» на русском языке."
        return result


class SilentAgent:
    """An agent that returns no artifact for any work item on every attempt."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    def generate(self, work_items: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls.append(list(work_items))
        return {}


class RaisingAgent:
    """An agent that fails with a process-style exception on every attempt."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    def generate(self, work_items: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls.append(list(work_items))
        raise RuntimeError("agent process failed")


class FlakyAgent:
    """An agent whose first attempt fails, then returns grounded evidence."""

    def __init__(self, pages: dict[str, dict[str, Any]]) -> None:
        self._pages = pages
        self.calls: list[list[dict[str, Any]]] = []

    def generate(self, work_items: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls.append(list(work_items))
        if len(self.calls) == 1:
            raise TimeoutError("agent timeout")
        return ReplayDescriptionAgent(self._pages).generate(work_items)


class InvalidGroundedProseAgent:
    """An agent whose grounding evidence is valid but whose grounded
    description prose is invalid: English-only or copied text, or the
    literal standard fallback string."""

    def __init__(self, description: str) -> None:
        self._description = description
        self.calls: list[list[dict[str, Any]]] = []

    @staticmethod
    def _evidence_urls(work_item: dict[str, Any]) -> list[str]:
        urls: list[str] = []
        for evidence in work_item.get("metadata", {}).get("source_evidence", []):
            if not isinstance(evidence, dict):
                continue
            url = str(evidence.get("evidence_url") or evidence.get("source_url", "")).strip()
            if url:
                urls.append(url)
        return urls

    def generate(self, work_items: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls.append(list(work_items))
        result: dict[str, Any] = {}
        for work_item in work_items:
            identity_key = str(work_item.get("identity_key", ""))
            urls = self._evidence_urls(work_item)
            is_audiobooks = any(
                "audiobooks.com" in str(evidence.get("source_name", "")).casefold()
                for evidence in work_item.get("metadata", {}).get("source_evidence", [])
            )
            if is_audiobooks:
                if identity_key == KINDRED_ID:
                    result[identity_key] = {
                        "content_description": self._description,
                        "grounding_evidence": {
                            "evidence_url": KINDRED_URL,
                            "evidence_available": True,
                            "subject": "Kindred novel.",
                            "work_type": "fiction",
                            "genre": "Science Fiction",
                        },
                    }
                else:
                    result[identity_key] = {
                        "content_description": "",
                        "grounding_evidence": {"evidence_url": urls[0] if urls else "", "evidence_available": False},
                    }
            else:
                result[identity_key] = f"Описание книги «{work_item.get('title', '')}» на русском языке."
        return result


class FantasyGenreAgent:
    """An agent whose authoritative evidence names a fiction genre without the
    word "fiction" (e.g. "Fantasy")."""

    def generate(self, work_items: list[dict[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for work_item in work_items:
            identity_key = str(work_item.get("identity_key", ""))
            if identity_key == KINDRED_ID:
                result[identity_key] = {
                    "content_description": "Роман о наследии рабства.",
                    "grounding_evidence": {
                        "evidence_url": KINDRED_URL,
                        "evidence_available": True,
                        "subject": "Kindred novel.",
                        "work_type": "fiction",
                        "genre": "Fantasy",
                    },
                }
            elif identity_key == GHOST_ID:
                result[identity_key] = {
                    "content_description": "",
                    "grounding_evidence": {"evidence_url": GHOST_URL, "evidence_available": False},
                }
            else:
                result[identity_key] = f"Описание книги «{work_item.get('title', '')}» на русском языке."
        return result


# ── Acceptance tests ─────────────────────────────────────────────────────


def test_fixtures_are_metadata_only_and_local() -> None:
    """Extraction replay entries carry metadata only, never copied prose."""
    extraction = _read_json(FIXTURES / "audiobooks-extraction.json")
    mentions = extraction["mentions"]
    assert [m["title"] for m in mentions] == ["Kindred", "Ghost Girl"]
    for mention in mentions:
        assert not (set(mention) & FORBIDDEN_COLLECTOR_RESPONSE_FIELDS)
        assert "content_description" not in mention
        assert mention["candidate_category"] == "adult nonfiction"
        assert mention["underlying_work_type"] == "book"
        assert mention["publication_status"] == "published"
        assert "/audiobook/" in mention["evidence_url"]

    capture_html = _read_json(FIXTURES / "audiobooks-capture.json")["html"]
    for mention in mentions:
        assert mention["evidence_url"] in capture_html

    for page_file in ("kindred-authoritative-page.json", "ghost-girl-authoritative-page.json"):
        page = _read_json(FIXTURES / page_file)
        assert set(page) <= {
            "evidence_url",
            "available",
            "title",
            "primary_author",
            "work_type",
            "genre",
            "publication_year",
            "publisher",
            "subject",
            "content_description",
        }

    kindred_page = _read_json(FIXTURES / "kindred-authoritative-page.json")
    assert kindred_page["work_type"] == "fiction"
    assert kindred_page["genre"] == "Science Fiction"
    assert kindred_page["subject"]  # transient acceptance-gate evidence


def test_grounded_audiobooks_description_flow(tmp_path: Path) -> None:
    """Reliable evidence enriches Kindred, unavailable evidence falls back,
    pure fiction is excluded, and non-Audiobooks behavior is unchanged."""
    config_path, output_dir = _setup_run(tmp_path)
    pages = _load_pages()
    agent = ReplayDescriptionAgent(pages)

    result = run_weekly_workflow(config_path, output_dir, adapters=_make_adapters(agent))

    assert result.outcome in {WorkflowOutcome.COMPLETED_NO_ATTENTION, WorkflowOutcome.COMPLETED_ATTENTION_REQUIRED}
    assert result.review_available

    # ── Scouting Candidates: Kindred removed, others kept ──
    candidates = _read_json(output_dir / "scouting-candidates.json")
    candidate_ids = {str(candidate["identity_key"]) for candidate in candidates}
    assert KINDRED_ID not in candidate_ids
    assert GHOST_ID in candidate_ids
    assert TEST_BOOK_ID in candidate_ids

    # ── Candidate history ──
    history = _read_json(output_dir / "candidate-history.json")
    by_id = {str(record["identity_key"]): record for record in history}

    kindred = by_id[KINDRED_ID]
    expected_description = pages[KINDRED_URL]["content_description"]
    assert kindred["content_description"] == expected_description
    validated, rejection = validate_content_description(expected_description)
    assert validated is not None, rejection
    provenance = kindred["description_provenance"]
    assert provenance["provenance"] == "codex-generated"
    # The supporting Evidence URL is preserved in description provenance.
    assert provenance["grounding_evidence_url"] == KINDRED_URL
    assert KINDRED_URL in provenance["evidence_basis"]
    # Structured authoritative genre/work-type metadata replaces the retailer
    # category; the free-form subject is never persisted.
    assert kindred["authoritative_work_type"] == "fiction"
    assert kindred["authoritative_genre"] == "Science Fiction"
    assert kindred["candidate_category"] == "science fiction"
    assert "authoritative_subject" not in kindred
    # An accepted grounded description clears any stale rejection reason.
    assert "content_description_rejection_reason" not in kindred
    # The Source Mention itself is unchanged.
    assert kindred["source_evidence"][0]["source_name"] == AUDIOBOOKS_NAME
    assert kindred["source_evidence"][0]["evidence_url"] == KINDRED_URL
    assert kindred["source_evidence"][0]["signal_type"] == "audiobook ranking"
    extraction_mentions = _read_json(FIXTURES / "audiobooks-extraction.json")["mentions"]
    kindred_mention = next(m for m in extraction_mentions if m["title"] == "Kindred")
    assert kindred["source_evidence"][0]["evidence_note"] == kindred_mention["evidence_note"]

    ghost = by_id[GHOST_ID]
    assert ghost["content_description"] == DESCRIPTION_FALLBACK
    assert ghost["description_provenance"]["provenance"] == "fallback"
    # The bounded fallback reason is persisted with the standard fallback.
    assert ghost["content_description_rejection_reason"] == GROUNDING_REASON_UNAVAILABLE

    test_book = by_id[TEST_BOOK_ID]
    assert test_book["content_description"] == TEST_BOOK_DESCRIPTION
    assert "authoritative_work_type" not in test_book

    # ── Internal exclusion record for the authoritative pure-fiction result ──
    exclusions = _read_json(output_dir / "internal-exclusions.json")
    assert len(exclusions) == 1
    exclusion = exclusions[0]
    assert exclusion["identity_key"] == KINDRED_ID
    assert exclusion["exclusion_reason"] == "fiction"
    assert exclusion["exclusion_date"] == "2000-01-01"
    assert exclusion["authoritative_work_type"] == "fiction"
    assert exclusion["authoritative_genre"] == "Science Fiction"
    assert "authoritative_subject" not in exclusion
    assert exclusion["grounding_evidence_url"] == KINDRED_URL

    # ── Diagnostic: explicit bounded reason for the unavailable evidence ──
    diagnostic = _read_json(output_dir / "description-enrichment.json")
    assert diagnostic["grounding_rejection_reasons"] == {GHOST_ID: GROUNDING_REASON_UNAVAILABLE}
    assert diagnostic["excluded_fiction_ids"] == [KINDRED_ID]
    assert GHOST_ID in diagnostic["fallback_ids"]
    assert set(diagnostic["applied_ids"]) == {KINDRED_ID, TEST_BOOK_ID}
    # Accepted IDs are never re-requested on retry.
    assert diagnostic["attempt_log"][0]["requested_count"] == 3
    assert diagnostic["attempt_log"][1]["requested_ids"] == [GHOST_ID]

    # ── Review and coverage: fallback reason exposed, exclusion visible ──
    review_md = (output_dir / "weekly-review.md").read_text(encoding="utf-8")
    assert "Исключена из отбора: художественная литература" in review_md
    coverage = _read_json(output_dir / "description-coverage.json")
    assert coverage["invalid_reason_counts"] == {GROUNDING_REASON_UNAVAILABLE: 1}
    with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    kindred_row = next(row for row in rows if row["Candidate ID"] == KINDRED_ID)
    assert kindred_row["Lifecycle"] == "Previously seen"
    assert "Excluded from acquisition: fiction" in kindred_row["Review context"]
    ghost_row = next(row for row in rows if row["Candidate ID"] == GHOST_ID)
    assert ghost_row["Lifecycle"] == "New"
    assert "Excluded from acquisition" not in ghost_row["Review context"]
    # The excluded work is not reported as a current editor-facing count, and
    # the post-enrichment description coverage is written back to run-summary.
    run_summary = _read_json(output_dir / "run-summary.json")
    assert run_summary["editor_facing_count"] == 2
    assert run_summary["update_delta_counts"] == {"new": 2, "upgraded": 0, "changed": 0, "unchanged": 0}
    assert run_summary["description_coverage"]["fallback_count"] == 1
    assert run_summary["description_coverage"]["invalid_reason_counts"] == {GROUNDING_REASON_UNAVAILABLE: 1}

    # ── All derived artifacts agree with the final candidates ──
    assert run_summary["excluded_candidate_count"] == 1
    contributions = {
        entry["source_name"]: entry["editor_facing_contribution"] for entry in run_summary["source_contributions"]
    }
    assert contributions[AUDIOBOOKS_NAME] == 1
    assert contributions["Test Source"] == 1
    assert any("2 editor-facing rows produced" in warning for warning in run_summary["major_warnings"])
    assert not any("3 editor-facing rows produced" in warning for warning in run_summary["major_warnings"])
    pilot_status = _read_json(output_dir / "pilot-status.json")
    assert pilot_status["editor_facing_count"] == 2
    assert pilot_status["readiness_basis"]["editor_facing_count"] == 2
    assert pilot_status["readiness_basis"]["major_warning_count"] == 1
    source_runs = _read_json(output_dir / "source-runs.json")
    audiobooks_run = next(run for run in source_runs if run["source_name"] == AUDIOBOOKS_NAME)
    assert audiobooks_run["editor_facing_count"] == 1
    assert audiobooks_run["excluded_candidate_count"] == 1
    source_metrics = _read_json(output_dir / "source-metrics.json")
    audiobooks_metric = next(metric for metric in source_metrics["metrics"] if metric["source_name"] == AUDIOBOOKS_NAME)
    assert audiobooks_metric["editor_facing_count"] == 1
    assert (output_dir / "source-review.md").exists()
    assert AUDIOBOOKS_NAME in (output_dir / "source-review.md").read_text(encoding="utf-8")

    # ── The free-form subject never reaches state or editor-facing files ──
    subject_evidence = pages[KINDRED_URL]["subject"]
    for artifact_name in (
        "source-mentions.json",
        "candidate-history.json",
        "scouting-candidates.json",
        "internal-exclusions.json",
        "description-enrichment.json",
        "editor-facing.csv",
        "weekly-review.md",
    ):
        text = (output_dir / artifact_name).read_text(encoding="utf-8")
        assert subject_evidence not in text, artifact_name
        assert "authoritative_subject" not in text, artifact_name

    # ── The agent saw the grounding contract on encoded items only via the
    # raw work items: the Audiobooks items were detected by their evidence ──
    assert len(agent.calls) == 3
    assert {str(wi["identity_key"]) for wi in agent.calls[0]} == {KINDRED_ID, GHOST_ID, TEST_BOOK_ID}


def test_title_alone_cannot_create_non_fallback_description(tmp_path: Path) -> None:
    """A title-derived string for an Audiobooks.com item yields the fallback
    with an explicit bounded reason, never a codex-generated description."""
    config_path, output_dir = _setup_run(tmp_path)
    agent = TitleOnlyAgent()

    run_weekly_workflow(config_path, output_dir, adapters=_make_adapters(agent))

    history = _read_json(output_dir / "candidate-history.json")
    by_id = {str(record["identity_key"]): record for record in history}
    for identity_key in (KINDRED_ID, GHOST_ID):
        record = by_id[identity_key]
        assert record["content_description"] == DESCRIPTION_FALLBACK
        assert record["description_provenance"]["provenance"] == "fallback"
        assert "authoritative_work_type" not in record
        assert record["candidate_category"] == "adult nonfiction"
        # The bounded reason persists with the standard fallback.
        assert record["content_description_rejection_reason"] == GROUNDING_REASON_MISSING_EVIDENCE

    # The same bounded reason reaches the scouting candidates and coverage.
    candidates = _read_json(output_dir / "scouting-candidates.json")
    for candidate in candidates:
        if candidate["identity_key"] in (KINDRED_ID, GHOST_ID):
            assert candidate["content_description_rejection_reason"] == GROUNDING_REASON_MISSING_EVIDENCE
    coverage = _read_json(output_dir / "description-coverage.json")
    assert coverage["invalid_reason_counts"] == {GROUNDING_REASON_MISSING_EVIDENCE: 2}

    # The non-Audiobooks item keeps the legacy plain-string contract, even
    # when the string is title-derived: grounding applies only to
    # Audiobooks.com items.
    assert by_id[TEST_BOOK_ID]["content_description"] == "Название задаёт тематический фокус: Test Book."

    # The Audiobooks title-derived prose never reaches state or diagnostics.
    for artifact_name in (
        "candidate-history.json",
        "scouting-candidates.json",
        "description-enrichment.json",
        "weekly-review.md",
        "editor-facing.csv",
    ):
        text = (output_dir / artifact_name).read_text(encoding="utf-8")
        assert "тематический фокус: Kindred" not in text, artifact_name
        assert "тематический фокус: Ghost Girl" not in text, artifact_name

    diagnostic = _read_json(output_dir / "description-enrichment.json")
    assert diagnostic["grounding_rejection_reasons"] == {
        KINDRED_ID: GROUNDING_REASON_MISSING_EVIDENCE,
        GHOST_ID: GROUNDING_REASON_MISSING_EVIDENCE,
    }
    # No authoritative fiction was established, so nothing is excluded.
    assert diagnostic["excluded_fiction_ids"] == []
    assert (
        not (output_dir / "internal-exclusions.json").exists()
        or _read_json(output_dir / "internal-exclusions.json") == []
    )

    # Both Audiobooks items remain scouting candidates (with fallbacks).
    candidates = _read_json(output_dir / "scouting-candidates.json")
    candidate_ids = {str(candidate["identity_key"]) for candidate in candidates}
    assert KINDRED_ID in candidate_ids
    assert GHOST_ID in candidate_ids


def test_unsupported_evidence_url_yields_fallback_with_reason(tmp_path: Path) -> None:
    """Grounding evidence tied to a URL outside the allowed set is rejected."""
    config_path, output_dir = _setup_run(tmp_path)

    run_weekly_workflow(config_path, output_dir, adapters=_make_adapters(WrongUrlAgent()))

    history = _read_json(output_dir / "candidate-history.json")
    kindred = next(record for record in history if str(record["identity_key"]) == KINDRED_ID)
    assert kindred["content_description"] == DESCRIPTION_FALLBACK
    assert "authoritative_work_type" not in kindred
    assert kindred["candidate_category"] == "adult nonfiction"
    assert kindred["content_description_rejection_reason"] == GROUNDING_REASON_UNSUPPORTED_URL

    diagnostic = _read_json(output_dir / "description-enrichment.json")
    assert diagnostic["grounding_rejection_reasons"] == {
        KINDRED_ID: GROUNDING_REASON_UNSUPPORTED_URL,
        GHOST_ID: GROUNDING_REASON_MISSING_EVIDENCE,
    }
    assert diagnostic["excluded_fiction_ids"] == []


def test_grounded_evidence_without_description_is_fallback_with_reason(tmp_path: Path) -> None:
    """Valid grounding evidence with an empty description is not success:
    the item resolves to the standard fallback with an explicit bounded reason
    and no authoritative metadata is applied."""
    config_path, output_dir = _setup_run(tmp_path)

    run_weekly_workflow(config_path, output_dir, adapters=_make_adapters(EmptyDescriptionAgent()))

    history = _read_json(output_dir / "candidate-history.json")
    kindred = next(record for record in history if str(record["identity_key"]) == KINDRED_ID)
    assert kindred["content_description"] == DESCRIPTION_FALLBACK
    assert kindred["description_provenance"]["provenance"] == "fallback"
    assert kindred["content_description_rejection_reason"] == GROUNDING_REASON_NO_DESCRIPTION
    assert "authoritative_work_type" not in kindred
    assert "authoritative_genre" not in kindred
    assert kindred["candidate_category"] == "adult nonfiction"

    diagnostic = _read_json(output_dir / "description-enrichment.json")
    assert diagnostic["grounding_rejection_reasons"] == {
        KINDRED_ID: GROUNDING_REASON_NO_DESCRIPTION,
        GHOST_ID: GROUNDING_REASON_MISSING_EVIDENCE,
    }
    assert diagnostic["excluded_fiction_ids"] == []
    assert _read_json(output_dir / "internal-exclusions.json") == []

    # Kindred stays a current scouting candidate with the fallback.
    candidates = _read_json(output_dir / "scouting-candidates.json")
    assert KINDRED_ID in {str(candidate["identity_key"]) for candidate in candidates}


@pytest.mark.parametrize(
    "invalid_description",
    [
        # English-only prose fails the Russian-prose contract.
        "Kindred is the story of Dana, a Black woman who is pulled back in time "
        "to the antebellum South to save the life of her white ancestor Rufus.",
        # Copied publisher/review-style English prose fails the copied-text check.
        "This is a powerful and remarkable novel that every reader should discover "
        "according to the advance praise printed on the jacket.",
        # Copied publisher/review prose inside Russian text fails the copied-text
        # check while still being Russian-majority text.
        "Издательство рекомендует книгу: по данным Publisher Weekly и Booklist, а также "
        "по материалам advance praise и review copy, роман получил высокие оценки.",
        # The literal standard fallback is not grounded prose either.
        DESCRIPTION_FALLBACK,
        # Whitespace-normalized fallback variants: validation folds the text
        # to the literal fallback, which must still be rejected.
        "Описание требует\nдополнительной проверки",
        "  \nОписание требует\nдополнительной проверки  \n",
    ],
)
def test_invalid_grounded_description_prose_is_fallback_with_reason(tmp_path: Path, invalid_description: str) -> None:
    """Valid grounding evidence with invalid description prose - English-only,
    copied text, the literal standard fallback, or a whitespace-normalized
    fallback variant - never passes the grounding gate: the item resolves to
    the standard fallback with an explicit bounded reason, and no
    authoritative metadata or fiction exclusion is applied."""
    config_path, output_dir = _setup_run(tmp_path)

    run_weekly_workflow(
        config_path, output_dir, adapters=_make_adapters(InvalidGroundedProseAgent(invalid_description))
    )

    history = _read_json(output_dir / "candidate-history.json")
    by_id = {str(record["identity_key"]): record for record in history}
    kindred = by_id[KINDRED_ID]
    # The final state contains the standard fallback, an explicit reason, and
    # fallback provenance - never the invalid prose.
    assert kindred["content_description"] == DESCRIPTION_FALLBACK
    assert kindred["description_provenance"]["provenance"] == "fallback"
    assert kindred["content_description_rejection_reason"] == GROUNDING_REASON_INVALID_PROSE
    assert "authoritative_work_type" not in kindred
    assert "authoritative_genre" not in kindred
    assert kindred["candidate_category"] == "adult nonfiction"
    # No authoritative fiction was established, so nothing is excluded and the
    # candidate stays current with the fallback.
    diagnostic = _read_json(output_dir / "description-enrichment.json")
    assert diagnostic["grounding_rejection_reasons"] == {
        KINDRED_ID: GROUNDING_REASON_INVALID_PROSE,
        GHOST_ID: GROUNDING_REASON_UNAVAILABLE,
    }
    assert diagnostic["excluded_fiction_ids"] == []
    assert (
        not (output_dir / "internal-exclusions.json").exists()
        or _read_json(output_dir / "internal-exclusions.json") == []
    )
    candidates = _read_json(output_dir / "scouting-candidates.json")
    candidate_ids = {str(candidate["identity_key"]) for candidate in candidates}
    assert KINDRED_ID in candidate_ids
    # The rejected prose never reaches state or diagnostics. The literal
    # fallback string is the persisted fallback itself, so only the rejected
    # English/copied prose is checked for leaks.
    if invalid_description != DESCRIPTION_FALLBACK:
        for artifact_name in (
            "candidate-history.json",
            "scouting-candidates.json",
            "description-enrichment.json",
            "weekly-review.md",
            "editor-facing.csv",
        ):
            text = (output_dir / artifact_name).read_text(encoding="utf-8")
            assert invalid_description not in text, artifact_name


def test_silent_agent_yields_fallback_with_bounded_reason(tmp_path: Path) -> None:
    """An empty agent response leaves every Audiobooks.com item with the
    standard fallback and the bounded agent-failure reason in candidate state
    and in the diagnostic reason map; non-Audiobooks items keep the legacy
    behavior without any rejection reason."""
    config_path, output_dir = _setup_run(tmp_path)
    agent = SilentAgent()

    run_weekly_workflow(config_path, output_dir, adapters=_make_adapters(agent))

    history = _read_json(output_dir / "candidate-history.json")
    by_id = {str(record["identity_key"]): record for record in history}
    for identity_key in (KINDRED_ID, GHOST_ID):
        record = by_id[identity_key]
        assert record["content_description"] == DESCRIPTION_FALLBACK
        assert record["description_provenance"]["provenance"] == "fallback"
        assert record["content_description_rejection_reason"] == GROUNDING_REASON_AGENT_FAILURE
        assert "authoritative_work_type" not in record
        assert "authoritative_genre" not in record
        assert record["candidate_category"] == "adult nonfiction"
    # Non-Audiobooks items keep the legacy fallback without a reason.
    test_book = by_id[TEST_BOOK_ID]
    assert test_book["content_description"] == DESCRIPTION_FALLBACK
    assert "content_description_rejection_reason" not in test_book

    diagnostic = _read_json(output_dir / "description-enrichment.json")
    assert diagnostic["grounding_rejection_reasons"] == {
        KINDRED_ID: GROUNDING_REASON_AGENT_FAILURE,
        GHOST_ID: GROUNDING_REASON_AGENT_FAILURE,
    }
    assert diagnostic["excluded_fiction_ids"] == []
    assert diagnostic["attempts"] == 3
    assert len(agent.calls) == 3

    # The bounded reason reaches coverage; all candidates stay current.
    coverage = _read_json(output_dir / "description-coverage.json")
    assert coverage["invalid_reason_counts"] == {GROUNDING_REASON_AGENT_FAILURE: 2}
    candidates = _read_json(output_dir / "scouting-candidates.json")
    candidate_ids = {str(candidate["identity_key"]) for candidate in candidates}
    assert {KINDRED_ID, GHOST_ID, TEST_BOOK_ID} <= candidate_ids


def test_failing_agent_yields_fallback_with_bounded_reason(tmp_path: Path) -> None:
    """A process-style exception on every attempt leaves each Audiobooks.com
    item with the standard fallback and the bounded agent-failure reason in
    the diagnostic reason map."""
    config_path, output_dir = _setup_run(tmp_path)
    agent = RaisingAgent()

    run_weekly_workflow(config_path, output_dir, adapters=_make_adapters(agent))

    history = _read_json(output_dir / "candidate-history.json")
    kindred = next(record for record in history if str(record["identity_key"]) == KINDRED_ID)
    assert kindred["content_description"] == DESCRIPTION_FALLBACK
    assert kindred["content_description_rejection_reason"] == GROUNDING_REASON_AGENT_FAILURE
    ghost = next(record for record in history if str(record["identity_key"]) == GHOST_ID)
    assert ghost["content_description"] == DESCRIPTION_FALLBACK
    assert ghost["content_description_rejection_reason"] == GROUNDING_REASON_AGENT_FAILURE

    diagnostic = _read_json(output_dir / "description-enrichment.json")
    assert diagnostic["grounding_rejection_reasons"] == {
        KINDRED_ID: GROUNDING_REASON_AGENT_FAILURE,
        GHOST_ID: GROUNDING_REASON_AGENT_FAILURE,
    }
    assert diagnostic["attempts"] == 3
    assert len(agent.calls) == 3


def test_valid_retry_clears_transient_rejection(tmp_path: Path) -> None:
    """A timeout on the first attempt records the bounded agent-failure
    reason; the valid retry accepts the grounded evidence and clears the
    transient rejection, replacing it with the retry's own reason where the
    retry still fails."""
    config_path, output_dir = _setup_run(tmp_path)
    agent = FlakyAgent(_load_pages())

    run_weekly_workflow(config_path, output_dir, adapters=_make_adapters(agent))

    # Attempt 1 times out, attempt 2 accepts Kindred and rejects Ghost Girl,
    # attempt 3 retries Ghost Girl alone.
    assert len(agent.calls) == 3
    history = _read_json(output_dir / "candidate-history.json")
    by_id = {str(record["identity_key"]): record for record in history}
    # The valid retry cleared the transient rejection for Kindred.
    kindred = by_id[KINDRED_ID]
    assert kindred["content_description"] != DESCRIPTION_FALLBACK
    assert "content_description_rejection_reason" not in kindred
    assert kindred["authoritative_work_type"] == "fiction"
    # Ghost Girl still fails on the retry, with the retry's own reason.
    ghost = by_id[GHOST_ID]
    assert ghost["content_description"] == DESCRIPTION_FALLBACK
    assert ghost["content_description_rejection_reason"] == GROUNDING_REASON_UNAVAILABLE

    diagnostic = _read_json(output_dir / "description-enrichment.json")
    assert diagnostic["grounding_rejection_reasons"] == {GHOST_ID: GROUNDING_REASON_UNAVAILABLE}
    assert diagnostic["excluded_fiction_ids"] == [KINDRED_ID]
    assert diagnostic["attempt_log"][0]["grounding_rejected_ids"] == [GHOST_ID, KINDRED_ID]
    assert diagnostic["attempt_log"][1]["grounding_rejected_ids"] == [GHOST_ID]
    assert diagnostic["attempt_log"][2]["grounding_rejected_ids"] == [GHOST_ID]


def test_canonical_exclusion_handles_fiction_genres() -> None:
    """The canonical exclusion rule recognizes fiction genres while leaving
    nonfiction categories eligible, keeping the exact "fiction" reason."""
    for category, expected in [
        ("Science Fiction", "fiction"),
        ("science-fiction", "fiction"),
        ("science_fiction", "fiction"),
        ("fiction", "fiction"),
        ("adult nonfiction", ""),
        ("non-fiction", ""),
        ("nonfiction", ""),
        ("creative nonfiction", ""),
        ("history", ""),
    ]:
        reason = ScoutingCandidate.exclusion_reason_for_mapping({"candidate_category": category})
        assert reason == expected, f"{category!r} -> {reason!r}, expected {expected!r}"

    assert normalized_genre_category("Science Fiction") == "science fiction"
    assert normalized_genre_category("science-fiction") == "science fiction"
    assert is_fiction_genre_category("Science Fiction") is True
    assert is_fiction_genre_category("non-fiction") is False
    assert is_fiction_genre_category("adult nonfiction") is False


def test_authoritative_work_type_controls_fiction_only_exclusion() -> None:
    """Authoritative work type decides pure-fiction eligibility: fiction is
    excluded regardless of genre wording, nonfiction stays eligible even when
    the genre or a stale retailer category says "fiction", and unrelated
    exclusions stay intact."""
    for mapping, expected in [
        # Authoritative fiction is excluded even when the genre lacks "fiction".
        ({"candidate_category": "Fantasy", "authoritative_work_type": "fiction"}, "fiction"),
        ({"candidate_category": "Thriller", "authoritative_work_type": "FICTION"}, "fiction"),
        # Authoritative nonfiction wins over fiction-named genres.
        ({"candidate_category": "Science Fiction Studies", "authoritative_work_type": "nonfiction"}, ""),
        ({"candidate_category": "science fiction studies", "authoritative_work_type": "nonfiction"}, ""),
        ({"candidate_category": "science fiction", "authoritative_work_type": "nonfiction"}, ""),
        # A stale retailer "fiction" category must not override nonfiction.
        ({"candidate_category": "fiction", "authoritative_work_type": "nonfiction"}, ""),
        ({"candidate_category": "history", "authoritative_work_type": "nonfiction"}, ""),
        # Unrelated exclusions stay intact under authoritative metadata.
        ({"candidate_category": "children", "authoritative_work_type": "nonfiction"}, "children"),
        ({"candidate_category": "topic-only", "authoritative_work_type": "fiction"}, "topic-only"),
        ({"candidate_category": "ya", "authoritative_work_type": "nonfiction"}, "ya"),
    ]:
        reason = ScoutingCandidate.exclusion_reason_for_mapping(mapping)
        assert reason == expected, f"{mapping!r} -> {reason!r}, expected {expected!r}"


def test_authoritative_eligibility_reapplied_across_runs() -> None:
    """Reconciliation reapplies authoritative work type and genre whenever
    either is present, so eligibility cannot change without new evidence."""
    from autoscout.pipeline import build_scouting_candidates

    def _mention(title: str) -> dict[str, Any]:
        return {
            "title": title,
            "primary_authors": ["Some Author"],
            "source_url": AUDIOBOOKS_URL,
            "evidence_url": f"https://www.audiobooks.com/audiobook/{title.lower()}/1",
            "signal_type": "audiobook ranking",
            "signal_strength": "minor mention",
            "evidence_note": "Listed.",
            "candidate_category": "adult nonfiction",
            "underlying_work_type": "book",
            "publication_status": "published",
        }

    # Later run: authoritative nonfiction with a fiction-named genre stays
    # eligible even though the current retailer category says "adult nonfiction"
    # and would otherwise look like the first run.
    studies_history = [
        {
            "identity_key": "studies|some author",
            "title": "Studies",
            "primary_authors": ["Some Author"],
            "authoritative_work_type": "nonfiction",
            "authoritative_genre": "Science Fiction Studies",
            "candidate_category": "science fiction studies",
        }
    ]
    kept, exclusions = build_scouting_candidates(
        [_mention("Studies")],
        "2000-01-01",
        russian_edition_checks=[],
        excluded_candidate_history=[],
        candidate_history=studies_history,
    )
    assert [candidate.identity_key() for candidate in kept] == ["studies|some author"]
    assert exclusions == []
    assert kept[0].get("authoritative_work_type") == "nonfiction"
    assert kept[0].get("authoritative_genre") == "Science Fiction Studies"
    assert kept[0].get("candidate_category") == "science fiction studies"

    # Later run: authoritative fiction with a genre that lacks "fiction" is
    # re-excluded through the canonical rule, and the exclusion record keeps
    # the bounded authoritative metadata.
    fantasy_history = [
        {
            "identity_key": "dragon|some author",
            "title": "Dragon",
            "primary_authors": ["Some Author"],
            "authoritative_work_type": "fiction",
            "authoritative_genre": "Fantasy",
            "candidate_category": "fantasy",
        }
    ]
    kept, exclusions = build_scouting_candidates(
        [_mention("Dragon")],
        "2000-01-01",
        russian_edition_checks=[],
        excluded_candidate_history=[],
        candidate_history=fantasy_history,
    )
    assert kept == []
    assert len(exclusions) == 1
    assert exclusions[0]["identity_key"] == "dragon|some author"
    assert exclusions[0]["exclusion_reason"] == "fiction"
    assert exclusions[0]["authoritative_work_type"] == "fiction"
    assert exclusions[0]["authoritative_genre"] == "Fantasy"
    assert "authoritative_subject" not in exclusions[0]

    # Later run: authoritative work type alone (no genre) is reapplied too.
    work_type_only_history = [
        {
            "identity_key": "novel|some author",
            "title": "Novel",
            "primary_authors": ["Some Author"],
            "authoritative_work_type": "fiction",
        }
    ]
    kept, exclusions = build_scouting_candidates(
        [_mention("Novel")],
        "2000-01-01",
        russian_edition_checks=[],
        excluded_candidate_history=[],
        candidate_history=work_type_only_history,
    )
    assert kept == []
    assert exclusions[0]["exclusion_reason"] == "fiction"
    assert exclusions[0]["authoritative_work_type"] == "fiction"


def test_fiction_with_fantasy_genre_excluded_on_first_run(tmp_path: Path) -> None:
    """Authoritative fiction is excluded on the first run even when the
    authoritative genre does not contain the word "fiction" (e.g. "Fantasy")."""
    config_path, output_dir = _setup_run(tmp_path)

    run_weekly_workflow(config_path, output_dir, adapters=_make_adapters(FantasyGenreAgent()))

    candidates = _read_json(output_dir / "scouting-candidates.json")
    assert KINDRED_ID not in {str(candidate["identity_key"]) for candidate in candidates}
    exclusions = _read_json(output_dir / "internal-exclusions.json")
    kindred_exclusion = next(record for record in exclusions if record["identity_key"] == KINDRED_ID)
    assert kindred_exclusion["exclusion_reason"] == "fiction"
    assert kindred_exclusion["authoritative_work_type"] == "fiction"
    assert kindred_exclusion["authoritative_genre"] == "Fantasy"
    history = _read_json(output_dir / "candidate-history.json")
    kindred = next(record for record in history if record["identity_key"] == KINDRED_ID)
    assert kindred["authoritative_genre"] == "Fantasy"
    assert kindred["candidate_category"] == "fantasy"


def test_search_optimization_trial_refreshed_after_authoritative_exclusion(tmp_path: Path) -> None:
    """With the search-optimization trial enabled, the post-enrichment refresh
    rebuilds the trial review from the final candidates: the excluded Kindred
    never appears as a current candidate in the refreshed artifacts."""
    config_path, output_dir = _setup_run(tmp_path, search_optimization_trial=True)

    run_weekly_workflow(config_path, output_dir, adapters=_make_adapters(ReplayDescriptionAgent(_load_pages())))

    review = _read_json(output_dir / "search-optimization-review.json")
    assert review["candidate_count"] == 2
    assert review["eligible_count"] == 2
    candidate_ids = {str(candidate["identity_key"]) for candidate in review["candidates"]}
    assert KINDRED_ID not in candidate_ids
    assert candidate_ids == {GHOST_ID, TEST_BOOK_ID}
    # The candidate-level table in the Markdown names only current candidates.
    review_md = (output_dir / "search-optimization-review.md").read_text(encoding="utf-8")
    table_section = review_md.split("## Candidate-Level Comparison", 1)[1].split("\n## ", 1)[0]
    assert "Kindred" not in table_section
    assert "Ghost Girl" in table_section
    assert "Test Book" in table_section

    # The bounded pre-suppression snapshot persisted by the pipeline keeps the
    # full pre-suppression evidence for the post-enrichment trial refresh:
    # Kindred is still present there (it was excluded after enrichment), with
    # its author and configured source identity (Issue #391).
    snapshot = _read_json(output_dir / "internal-pre-suppression-candidates.json")
    snapshot_by_id = {str(candidate["identity_key"]): candidate for candidate in snapshot}
    assert KINDRED_ID in snapshot_by_id
    assert snapshot_by_id[KINDRED_ID]["primary_authors"] == ["Octavia E. Butler"]
    assert snapshot_by_id[KINDRED_ID]["source_evidence"][0]["source_name"] == AUDIOBOOKS_NAME
    # The snapshot is bounded metadata: no description prose travels with it.
    for candidate in snapshot:
        for evidence in candidate.get("source_evidence", []):
            assert not str(evidence.get("content_description", "")).strip()


def test_nonfiction_genre_replaces_retailer_category() -> None:
    """An accepted nonfiction grounded artifact replaces the retailer category
    with the folded authoritative genre and persists structured metadata only."""
    record = {
        "identity_key": "sapiens|yuval noah harari",
        "candidate_category": "adult nonfiction",
        "content_description": "",
    }
    artifact = {
        "identity_key": "sapiens|yuval noah harari",
        "content_description": "Популярное исследование истории человечества.",
        "research_sources": [KINDRED_URL],
        "grounding_evidence_url": KINDRED_URL,
        "authoritative_work_type": "nonfiction",
        "authoritative_genre": "History",
        "rejection_reason": "",
    }
    updated, results = apply_description_artifacts([record], [artifact], provenance="codex-generated")
    updated_record = next(r for r in updated if r["identity_key"] == "sapiens|yuval noah harari")
    assert results["applied"] == 1
    assert updated_record["content_description"] == "Популярное исследование истории человечества."
    assert updated_record["authoritative_work_type"] == "nonfiction"
    assert updated_record["authoritative_genre"] == "History"
    assert updated_record["candidate_category"] == "history"
    assert "authoritative_subject" not in updated_record
    assert "content_description_rejection_reason" not in updated_record
    # Nonfiction genres are eligible under the canonical rule.
    assert ScoutingCandidate.exclusion_reason_for_mapping(updated_record) == ""

    # The same first-run path with a genre that contains the word "fiction":
    # authoritative nonfiction keeps the work eligible.
    studies_artifact = {
        "identity_key": "studies|some author",
        "content_description": "Исследование научной фантастики.",
        "research_sources": [KINDRED_URL],
        "grounding_evidence_url": KINDRED_URL,
        "authoritative_work_type": "nonfiction",
        "authoritative_genre": "Science Fiction Studies",
        "rejection_reason": "",
    }
    updated, results = apply_description_artifacts(
        [
            {
                "identity_key": "studies|some author",
                "candidate_category": "adult nonfiction",
                "content_description": "",
            }
        ],
        [studies_artifact],
        provenance="codex-generated",
    )
    studies_record = next(r for r in updated if r["identity_key"] == "studies|some author")
    assert studies_record["authoritative_genre"] == "Science Fiction Studies"
    assert studies_record["candidate_category"] == "science fiction studies"
    assert ScoutingCandidate.exclusion_reason_for_mapping(studies_record) == ""


def test_grounding_rejection_cleared_on_accept() -> None:
    """A stale rejection reason is cleared when a grounded description is
    accepted, and a fresh rejection reason is not kept on later success."""
    record = {
        "identity_key": KINDRED_ID,
        "candidate_category": "adult nonfiction",
        "content_description": "",
        "content_description_rejection_reason": GROUNDING_REASON_UNAVAILABLE,
    }
    artifact = {
        "identity_key": KINDRED_ID,
        "content_description": "Научно-фантастический роман о наследии рабства.",
        "research_sources": [KINDRED_URL],
        "grounding_evidence_url": KINDRED_URL,
        "authoritative_work_type": "fiction",
        "authoritative_genre": "Science Fiction",
        "rejection_reason": "",
    }
    updated, results = apply_description_artifacts([record], [artifact], provenance="codex-generated")
    updated_record = next(r for r in updated if r["identity_key"] == KINDRED_ID)
    assert results["applied"] == 1
    assert "content_description_rejection_reason" not in updated_record
    assert updated_record["content_description"] == "Научно-фантастический роман о наследии рабства."


def test_grounding_contract_never_admits_source_url_fallback() -> None:
    """A work item with only a source/category URL fails closed: the
    grounding contract exposes no allowed URLs, and any response tied to the
    category URL is rejected with the bounded unsupported reason."""
    from autoscout.description_enrichment import GROUNDING_REASON_MISSING_EVIDENCE
    from autoscout.description_enrichment import GROUNDING_REASON_UNSUPPORTED_URL
    from autoscout.description_enrichment import _encode_work_items
    from autoscout.description_enrichment import _grounding_contract_for_item
    from autoscout.description_enrichment import _grounding_rejection_reason

    source_url_only_item = {
        "identity_key": KINDRED_ID,
        "title": "Kindred",
        "metadata": {
            "source_evidence": [
                {
                    "source_name": AUDIOBOOKS_NAME,
                    "source_family": "audiobook ranking",
                    "source_url": AUDIOBOOKS_URL,
                    # No direct evidence_url on the Source Mention.
                }
            ]
        },
    }
    contract = _grounding_contract_for_item(source_url_only_item)
    assert contract is not None
    assert contract["allowed_evidence_urls"] == []

    # The model-facing encoding exposes no allowed URL either.
    encoded = _encode_work_items([source_url_only_item])[0]
    assert encoded["grounding_contract"]["allowed_evidence_urls"] == []

    # An agent response that tries to use the category URL fails closed.
    category_url_artifact = {
        "content_description": "Роман о наследии рабства.",
        "grounding_evidence": {
            "evidence_url": AUDIOBOOKS_URL,
            "evidence_available": True,
            "subject": "Kindred novel.",
            "work_type": "fiction",
            "genre": "Science Fiction",
        },
    }
    assert _grounding_rejection_reason(source_url_only_item, category_url_artifact) == GROUNDING_REASON_UNSUPPORTED_URL
    # A response without any grounding also fails closed.
    assert _grounding_rejection_reason(source_url_only_item, {"content_description": "Роман."}) == (
        GROUNDING_REASON_MISSING_EVIDENCE
    )

    # With both URLs present, only the direct evidence URL is allowed.
    full_item = {
        "identity_key": KINDRED_ID,
        "title": "Kindred",
        "metadata": {
            "source_evidence": [
                {
                    "source_name": AUDIOBOOKS_NAME,
                    "source_url": AUDIOBOOKS_URL,
                    "evidence_url": KINDRED_URL,
                }
            ]
        },
    }
    full_contract = _grounding_contract_for_item(full_item)
    assert full_contract["allowed_evidence_urls"] == [KINDRED_URL]
    assert AUDIOBOOKS_URL not in full_contract["allowed_evidence_urls"]


def test_history_snapshot_declares_and_preserves_authoritative_fields() -> None:
    """The pipeline history snapshot declares the authoritative enrichment
    fields explicitly and never overwrites them with absent current values."""
    from autoscout.pipeline import HISTORY_SNAPSHOT_FIELDS
    from autoscout.pipeline import update_candidate_history_snapshots

    for field in ("authoritative_genre", "authoritative_work_type", "content_description_rejection_reason"):
        assert field in HISTORY_SNAPSHOT_FIELDS, field

    history = [
        {
            "identity_key": KINDRED_ID,
            "candidate_category": "science fiction",
            "authoritative_genre": "Science Fiction",
            "authoritative_work_type": "fiction",
            "content_description_rejection_reason": GROUNDING_REASON_UNAVAILABLE,
            "content_description": "Описание книги.",
        }
    ]
    # The current candidate carries only the retailer category and no
    # authoritative fields: the stored values must survive the snapshot.
    candidate = {
        "identity_key": KINDRED_ID,
        "candidate_category": "adult nonfiction",
        "content_description": "Описание книги.",
    }
    updated = update_candidate_history_snapshots(history, [candidate], "2000-01-01")
    record = next(r for r in updated if str(r["identity_key"]) == KINDRED_ID)
    assert record["authoritative_genre"] == "Science Fiction"
    assert record["authoritative_work_type"] == "fiction"
    assert record["content_description_rejection_reason"] == GROUNDING_REASON_UNAVAILABLE
    assert record["candidate_category"] == "science fiction"


def test_authoritative_exclusion_survives_second_run(tmp_path: Path) -> None:
    """A second identical offline run with persistent state re-excludes
    Kindred from current Scouting Candidates without any model/network call.

    The pipeline reapplies the stored authoritative genre before the canonical
    exclusion check, and history snapshots preserve it, so the grounded
    fiction exclusion stays durable across runs.
    """
    pages = _load_pages()
    for index in (1, 2):
        run_dir = tmp_path / f"run-{index}"
        run_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        config_path = _write_config(run_dir)
        output_dir = run_dir / "output"
        output_dir.mkdir()
        (run_dir / "test-capture.json").write_text(
            json.dumps(
                {
                    "source_url": "https://example.test/source",
                    "html": '<html><body><a href="https://example.test/book">Link</a> by Test Author</body></html>',
                }
            ),
            encoding="utf-8",
        )
        adapters = deterministic_adapters()
        adapters.capture.prepare(config_path, output_dir, live=False)
        _write_extraction_files(output_dir)
        agent = ReplayDescriptionAgent(pages)
        result = run_weekly_workflow(config_path, output_dir, state_dir=state_dir, adapters=_make_adapters(agent))
        assert result.review_available, f"run {index}: workflow must complete"

        # Kindred is absent from current scouting candidates on both runs.
        candidates = _read_json(output_dir / "scouting-candidates.json")
        candidate_ids = {str(candidate["identity_key"]) for candidate in candidates}
        assert KINDRED_ID not in candidate_ids, f"run {index}: Kindred must stay excluded"
        assert GHOST_ID in candidate_ids, f"run {index}: Ghost Girl stays a current candidate"

        # Authoritative genre and folded category remain correct in history.
        history = read_state(state_dir, "candidate_history")
        kindred = next(record for record in history if str(record["identity_key"]) == KINDRED_ID)
        assert kindred["authoritative_genre"] == "Science Fiction", f"run {index}"
        assert kindred["authoritative_work_type"] == "fiction", f"run {index}"
        assert kindred["candidate_category"] == "science fiction", f"run {index}"
        assert "authoritative_subject" not in kindred, f"run {index}"
        # The valid grounded description and its provenance survive.
        assert kindred["description_provenance"]["provenance"] == "codex-generated", f"run {index}"

        # The exclusion record stays in cumulative excluded state with the
        # bounded structured metadata.
        excluded_state = read_state(state_dir, "excluded_candidate_history")
        kindred_excluded = next(record for record in excluded_state if str(record["identity_key"]) == KINDRED_ID)
        assert kindred_excluded["exclusion_reason"] == "fiction", f"run {index}"
        assert kindred_excluded.get("authoritative_genre") == "Science Fiction", f"run {index}"
        assert "authoritative_subject" not in kindred_excluded, f"run {index}"

        # The review shows the historical exclusion, not a current row.
        review_md = (output_dir / "weekly-review.md").read_text(encoding="utf-8")
        assert "Исключена из отбора: художественная литература" in review_md, f"run {index}"
        with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        kindred_row = next(row for row in rows if row["Candidate ID"] == KINDRED_ID)
        assert kindred_row["Lifecycle"] == "Previously seen", f"run {index}"

        # Retailer Source Mentions stay metadata-only on both runs.
        mentions = _read_json(output_dir / "source-mentions.json")
        for mention in mentions:
            assert not (set(mention) & FORBIDDEN_COLLECTOR_RESPONSE_FIELDS), f"run {index}"
            assert not str(mention.get("content_description", "")).strip(), f"run {index}"

    # The second run's own internal exclusion record also carries the genre.
    second_exclusions = _read_json(tmp_path / "run-2" / "output" / "internal-exclusions.json")
    kindred_second = next(record for record in second_exclusions if str(record["identity_key"]) == KINDRED_ID)
    assert kindred_second["exclusion_reason"] == "fiction"
    assert kindred_second.get("authoritative_genre") == "Science Fiction"


def test_no_live_network_or_model_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The deterministic replay flow never touches the network or a model."""

    def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("live network or model call attempted")

    monkeypatch.setattr(urllib.request, "urlopen", _explode)
    monkeypatch.setattr(subprocess, "run", _explode)

    import autoscout.description_enrichment as enrichment_module

    class _ExplodingCodexAgent:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("production Codex agent constructed in a replay test")

    monkeypatch.setattr(enrichment_module, "CodexDescriptionEnrichmentAgent", _ExplodingCodexAgent)

    config_path, output_dir = _setup_run(tmp_path)
    agent = ReplayDescriptionAgent(_load_pages())
    result = run_weekly_workflow(config_path, output_dir, adapters=_make_adapters(agent))

    assert result.review_available
    assert len(agent.calls) >= 1


def test_identical_replay_inputs_are_deterministic(tmp_path: Path) -> None:
    """Two identical runs produce byte-identical state and review artifacts."""
    runs: list[dict[str, str]] = []
    for index in (1, 2):
        run_dir = tmp_path / f"run-{index}"
        run_dir.mkdir()
        config_path = _write_config(run_dir)
        output_dir = run_dir / "output"
        output_dir.mkdir()
        (run_dir / "test-capture.json").write_text(
            json.dumps(
                {
                    "source_url": "https://example.test/source",
                    "html": '<html><body><a href="https://example.test/book">Link</a> by Test Author</body></html>',
                }
            ),
            encoding="utf-8",
        )
        adapters = deterministic_adapters()
        adapters.capture.prepare(config_path, output_dir, live=False)
        _write_extraction_files(output_dir)
        agent = ReplayDescriptionAgent(_load_pages())
        result = run_weekly_workflow(config_path, output_dir, adapters=_make_adapters(agent))
        assert result.review_available
        artifacts = {
            "scouting-candidates.json",
            "candidate-history.json",
            "internal-exclusions.json",
            "description-enrichment.json",
            "description-coverage.json",
            "source-mentions.json",
            "run-summary.json",
            "pilot-status.json",
            "source-runs.json",
            "source-metrics.json",
            "source-review.md",
            "editor-facing.csv",
            "weekly-review.md",
        }
        runs.append(
            {
                name: (output_dir / name).read_text(encoding="utf-8")
                for name in artifacts
                if (output_dir / name).exists()
            }
        )

    assert runs[0] == runs[1]
    assert "kindred|octavia e butler" in runs[0]["internal-exclusions.json"]


def test_encoded_work_items_carry_grounding_contract_only_for_audiobooks() -> None:
    """The model-facing encoding adds the grounding contract only to
    Audiobooks.com items and bounds the allowed evidence URLs."""
    from autoscout.description_enrichment import _encode_work_items

    def _item(identity_key: str, source_name: str, evidence_url: str) -> dict[str, Any]:
        return {
            "identity_key": identity_key,
            "title": "Some Book",
            "metadata": {
                "source_evidence": [
                    {
                        "source_name": source_name,
                        "source_family": "audiobook ranking",
                        "evidence_url": evidence_url,
                    }
                ]
            },
        }

    audiobooks_item = _item(KINDRED_ID, AUDIOBOOKS_NAME, KINDRED_URL)
    encoded = _encode_work_items([audiobooks_item])[0]
    contract = encoded["grounding_contract"]
    assert contract["source"] == "audiobooks.com"
    assert contract["allowed_evidence_urls"] == [KINDRED_URL]

    # An Audible chart item shares the family but is not Audiobooks.com.
    audible_item = _item(
        "some book|some author", "Audible Bestselling Science Audiobooks", "https://www.audible.example/pd/1"
    )
    assert "grounding_contract" not in _encode_work_items([audible_item])[0]

    # The production prompt states the grounding contract.
    from autoscout.description_enrichment import CodexDescriptionEnrichmentAgent

    agent = CodexDescriptionEnrichmentAgent(timeout_seconds=1)
    prompt_text = agent._build_prompt()
    assert "grounding_contract" in prompt_text
    assert "must not search, browse generally" in prompt_text
    assert "from the title alone" in prompt_text
    assert "byte for byte" in prompt_text
    assert '"genre": "<authoritative genre, e.g. Science Fiction>"' in prompt_text
