"""Offline contract tests for the grounded stage's startup and its gate.

The issue #468 policy ledger and its loader were retired in issue #1123:
owners come from ``configs/source-owner-policy.json``. These tests keep what
still holds: the offline stage needs no policy file, a configured stage
refuses to start without the owner policy, and two genuine reviews pass the
gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import GroundedDescriptionStageService
from autoscout.grounded_description import NoNetworkDiscoveryProvider
from autoscout.grounded_description import evaluate_grounding_gate
from autoscout.weekly_workflow import _make_production_enrichment


def test_offline_enrichment_does_not_require_policy_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTOSCOUT_GROUNDED_DISCOVERY_ENDPOINT", raising=False)

    enrichment = _make_production_enrichment(repo_path=tmp_path)

    assert isinstance(enrichment, GroundedDescriptionStageService)
    assert isinstance(enrichment.stage.discovery, NoNetworkDiscoveryProvider)
    assert enrichment.enabled is False


def test_configured_enrichment_requires_the_owner_policy_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _make_production_enrichment(repo_path=tmp_path, discovery_endpoint="https://search.example.test")


def _review_source(url: str, owner_id: str, subject_text: str) -> dict[str, Any]:
    return {
        "url": url,
        "source_kind": "review",
        "source_family": "trade_review",
        "owner_id": owner_id,
        "source_language": "en",
        "title": "The Work",
        "primary_authors": ["Alex Author"],
        "subject_text": subject_text,
        "subject_key": subject_text,
        "checked_date": "2026-09-01",
    }


def test_two_genuine_review_mappings_with_distinct_subject_text_are_accepted() -> None:
    target = DurableTarget("candidate-468", "The Work", ("Alex Author",), {})
    result = evaluate_grounding_gate(
        target,
        [
            _review_source(
                "https://www.publishersweekly.com/9780000000001",
                "publishers-weekly",
                "A history of memory and migration.",
            ),
            _review_source(
                "https://www.bookbrowse.com/reviews/all-thats-unseen",
                "bookbrowse",
                "This book examines migration through family archives.",
            ),
        ],
    )

    assert result.accepted
    assert len(result.accepted_sources) == 2
