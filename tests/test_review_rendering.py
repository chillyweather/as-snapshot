from __future__ import annotations

from itertools import permutations
from typing import Any

from autoscout.content_description import DESCRIPTION_BACKFILLED_STATUS
from autoscout.content_description import DESCRIPTION_GROUNDED_STATUS
from autoscout.content_description import DESCRIPTION_SINGLE_SOURCE_STATUS
from autoscout.review_rendering import assemble_cumulative_review

IDENTITY_KEY = "combined context|alex example"
ORIGINAL_SIGNAL_EVIDENCE = {
    "source_name": "External Signal List",
    "source_family": "media/curated list",
    "source_url": "https://example.test/external-signal",
    "evidence_url": "https://example.test/external-signal/item",
    "signal_strength": "major media list",
    "evidence_note": "Original external signal.",
}


def _history() -> list[dict[str, Any]]:
    return [
        {
            "identity_key": IDENTITY_KEY,
            "title": "Combined Context",
            "primary_authors": ["Alex Example"],
            "editor_status": "reject",
            "editor_note": "Not for us.",
            "last_seen_date": "2026-07-04",
            "russian_edition_status": "not found in Russian",
            "source_evidence": [ORIGINAL_SIGNAL_EVIDENCE, ORIGINAL_SIGNAL_EVIDENCE],
            "hidden_by_rule_id": "taste-0001",
            "hidden_by_rule_reason": "author metadata matched",
            "last_hidden_by_rule_date": "2026-07-04",
        }
    ]


def _exclusions() -> list[dict[str, Any]]:
    return [
        {
            "identity_key": IDENTITY_KEY,
            "title": "Combined Context",
            "primary_authors": ["Alex Example"],
            "exclusion_reason": "fiction",
            "exclusion_date": "2026-07-12",
            "evidence": [{"source_url": "https://example.test/fiction"}],
        },
        {
            "identity_key": IDENTITY_KEY,
            "title": "Combined Context",
            "primary_authors": ["Alex Example"],
            "exclusion_reason": "found in Russian",
            "exclusion_date": "2026-07-10",
            "russian_edition_url": "https://example.test/ru/older",
            "russian_edition_evidence": [
                {
                    "match_type": "same-work Russian-language edition",
                    "language": "Russian",
                    "url": "https://example.test/ru/older",
                }
            ],
            "evidence": [{"source_url": "https://example.test/ru/older"}],
        },
        {
            "identity_key": IDENTITY_KEY,
            "title": "Combined Context",
            "primary_authors": ["Alex Example"],
            "exclusion_reason": "found in Russian",
            "exclusion_date": "2026-07-11",
            "russian_edition_url": "https://example.test/ru/latest",
            "russian_edition_evidence": [
                {
                    "match_type": "same-work Russian-language edition",
                    "language": "Russian",
                    "url": "https://example.test/ru/latest",
                }
            ],
            "russian_edition_verification": {
                "check_coverage": "complete",
                "provider_outcome": "matched",
                "confidence": "high",
                "checked_sources": ["Fixture Provider"],
                "checked_at": "2026-07-11",
                "query_timestamp": "2026-07-11",
                "evidence_urls": ["https://example.test/ru/latest"],
                "providers": [
                    {
                        "name": "Fixture Provider",
                        "status": "checked",
                        "outcome": "matched",
                        "evidence_urls": ["https://example.test/ru/latest"],
                        "evidence": [
                            {
                                "match_type": "same-work Russian-language edition",
                                "language": "Russian",
                                "url": "https://example.test/ru/latest",
                            }
                        ],
                    }
                ],
                "reason": "",
            },
            "evidence": [{"source_url": "https://example.test/ru/latest"}],
        },
    ]


def _render_signature(exclusions: list[dict[str, Any]]) -> tuple[Any, ...]:
    rows, _summary = assemble_cumulative_review(
        current_candidates=[],
        candidate_history=_history(),
        current_editor_rows=[],
        suppressed_candidates=[
            {
                "identity_key": IDENTITY_KEY,
                "editor_status": "reject",
                "suppression_reason": "suppressed by editor status without Material Change",
            }
        ],
        excluded_candidate_history=exclusions[:1],
        current_exclusions=exclusions[1:],
    )
    row = rows[0]
    return (
        row["review_context"],
        row["russian"],
        row["russian_url"],
        row["editor_row"]["Russian-edition URL"],
        row["editor_row"]["Links"],
        row["_last_seen_date"],
    )


def test_combined_suppression_rule_and_exclusion_context_is_augmented():
    signature = _render_signature(_exclusions())

    assert signature[0] == (
        "Hard Rule taste-0001 applied on 2026-07-04: author metadata matched; "
        "Suppressed from current recommendations: editor status 'reject' "
        "(suppressed by editor status without Material Change).; "
        "Excluded from acquisition: found in Russian on 2026-07-11; "
        "evidence: https://example.test/ru/latest."
    )
    assert signature[1:4] == (
        "found in Russian",
        "https://example.test/ru/latest",
        "https://example.test/ru/latest",
    )
    assert "https://example.test/ru/older" in signature[4]
    links = signature[4].split(" | ")
    assert links.count("https://example.test/external-signal/item") == 1
    assert links.count("https://example.test/external-signal") == 1
    assert signature[5] == "2026-07-11"


def test_multiple_exclusions_merge_order_independently_and_keep_strongest_transition():
    signatures = {_render_signature(list(order)) for order in permutations(_exclusions())}

    assert len(signatures) == 1


def test_review_row_exposes_publisher_without_unverified_book_link():
    identity_key = "book|author"
    candidate = {
        "identity_key": identity_key,
        "title": "A Long Book Title",
        "primary_authors": ["Alex Example"],
        "publisher": "Example Press",
        "publication_year": "2026",
        "publisher_provenance": "https://publisher.example.test/book",
        "content_description": "Русское описание книги.",
        "source_evidence": [],
    }

    rows, _summary = assemble_cumulative_review(
        current_candidates=[candidate],
        candidate_history=[],
        current_editor_rows=[],
    )

    assert rows[0]["publisher"] == "Example Press"
    assert rows[0]["publication_year"] == "2026"
    assert "book_url" not in rows[0]


def test_review_row_does_not_treat_description_or_source_evidence_as_a_book_link():
    candidate = {
        "identity_key": "ranked book|alex example",
        "title": "Ranked Book",
        "primary_authors": ["Alex Example"],
        "content_description": "Русское описание книги.",
        "description_provenance": {
            "provenance": "backfilled",
            "evidence_basis": ["https://example.test/ranking"],
        },
        "source_evidence": [
            {
                "source_name": "Ranking",
                "source_url": "https://example.test/ranking",
                "evidence_url": "https://example.test/ranking",
            }
        ],
    }

    rows, _summary = assemble_cumulative_review(
        current_candidates=[candidate],
        candidate_history=[],
        current_editor_rows=[],
    )

    assert "book_url" not in rows[0]


def test_current_row_prefers_canonical_history_description_over_stale_snapshot():
    """A refreshed canonical description must replace an older run snapshot."""
    identity_key = "fresh description|alex example"
    candidate = {
        "identity_key": identity_key,
        "title": "Fresh Description",
        "primary_authors": ["Alex Example"],
        "content_description": "Старое описание из снимка запуска.",
        "source_evidence": [],
    }
    history = [
        {
            "identity_key": identity_key,
            "title": "Fresh Description",
            "primary_authors": ["Alex Example"],
            "content_description": "Новое каноническое описание из истории.",
            "description_provenance": {"provenance": "backfilled"},
        }
    ]

    rows, _summary = assemble_cumulative_review(
        current_candidates=[candidate],
        candidate_history=history,
        current_editor_rows=[],
    )

    assert rows[0]["content_description"] == "Новое каноническое описание из истории."


def test_backfilled_history_overrides_stale_not_attempted_stage_status():
    """A later backfill must replace a stale weekly stage status in the review."""
    identity_key = "backfilled status|alex example"
    candidate = {
        "candidate_id": "candidate-backfilled-status",
        "identity_key": identity_key,
        "title": "Backfilled Status",
        "primary_authors": ["Alex Example"],
        "content_description": "Старое описание из снимка запуска.",
        "source_evidence": [],
    }
    history = [
        {
            "candidate_id": "candidate-backfilled-status",
            "identity_key": identity_key,
            "title": "Backfilled Status",
            "primary_authors": ["Alex Example"],
            "content_description": "Новое каноническое описание из backfill.",
            "description_provenance": {
                "provenance": "backfilled",
                "evidence_basis": ["https://example.test/publisher"],
            },
        }
    ]
    stale_stage = {
        "outcomes": [
            {
                "candidate_id": "candidate-backfilled-status",
                "identity_key": identity_key,
                "outcome": "not_attempted",
                "reason_code": "not_attempted_stage_budget",
                "candidate_timing": {
                    "discovery_seconds": 1.0,
                    "fetch_seconds": 1.0,
                    "synthesis_seconds": None,
                    "total_seconds": 2.0,
                },
            }
        ]
    }

    rows, _summary = assemble_cumulative_review(
        current_candidates=[candidate],
        candidate_history=history,
        current_editor_rows=[],
        grounded_description_stage=stale_stage,
    )

    assert rows[0]["content_description"] == "Новое каноническое описание из backfill."
    # Issue #912 changed this rule: research sources are not checked pages,
    # so neither one nor two of them earns a grounded status.
    assert rows[0]["description_status"] == DESCRIPTION_BACKFILLED_STATUS

    history[0]["description_provenance"]["evidence_basis"].append("https://example.test/review")
    rows, _summary = assemble_cumulative_review(
        current_candidates=[candidate],
        candidate_history=history,
        current_editor_rows=[],
        grounded_description_stage=stale_stage,
    )
    assert rows[0]["description_status"] == DESCRIPTION_BACKFILLED_STATUS
    assert rows[0]["description_status"] not in {DESCRIPTION_GROUNDED_STATUS, DESCRIPTION_SINGLE_SOURCE_STATUS}


def test_current_row_exposes_added_from_history():
    """Current candidate row carries first_seen_date from Candidate History."""
    identity_key = "added proof|alex example"
    candidate = {
        "identity_key": identity_key,
        "title": "Added Proof",
        "primary_authors": ["Alex Example"],
        "source_evidence": [],
    }
    history = [
        {
            "identity_key": identity_key,
            "title": "Added Proof",
            "primary_authors": ["Alex Example"],
            "first_seen_date": "2026-05-15",
            "last_seen_date": "2026-07-01",
        }
    ]

    rows, _summary = assemble_cumulative_review(
        current_candidates=[candidate],
        candidate_history=history,
        current_editor_rows=[],
    )

    assert rows[0]["first_seen_date"] == "2026-05-15"


def test_historical_row_exposes_added_from_history():
    """Previously seen row carries first_seen_date from Candidate History."""
    identity_key = "historical added|alex example"
    history = [
        {
            "identity_key": identity_key,
            "title": "Historical Added",
            "primary_authors": ["Alex Example"],
            "first_seen_date": "2026-04-01",
            "last_seen_date": "2026-06-01",
        }
    ]

    rows, _summary = assemble_cumulative_review(
        current_candidates=[],
        candidate_history=history,
        current_editor_rows=[],
    )

    assert rows[0]["first_seen_date"] == "2026-04-01"
    assert rows[0]["lifecycle_label"] == "Previously seen"


def test_current_row_exposes_updated_from_history():
    """Current candidate row carries last_material_change_date when history has it."""
    identity_key = "updated proof|alex example"
    candidate = {
        "identity_key": identity_key,
        "title": "Updated Proof",
        "primary_authors": ["Alex Example"],
        "source_evidence": [],
    }
    history = [
        {
            "identity_key": identity_key,
            "title": "Updated Proof",
            "primary_authors": ["Alex Example"],
            "first_seen_date": "2026-05-01",
            "last_seen_date": "2026-07-01",
            "last_material_change_date": "2026-06-15",
        }
    ]

    rows, _summary = assemble_cumulative_review(
        current_candidates=[candidate],
        candidate_history=history,
        current_editor_rows=[],
    )

    assert rows[0]["last_material_change_date"] == "2026-06-15"


def test_current_row_omits_updated_when_history_has_none():
    """No fake Updated date for a candidate without a material change."""
    identity_key = "no change|alex example"
    candidate = {
        "identity_key": identity_key,
        "title": "No Change",
        "primary_authors": ["Alex Example"],
        "source_evidence": [],
    }
    history = [
        {
            "identity_key": identity_key,
            "title": "No Change",
            "primary_authors": ["Alex Example"],
            "first_seen_date": "2026-05-01",
            "last_seen_date": "2026-07-01",
        }
    ]

    rows, _summary = assemble_cumulative_review(
        current_candidates=[candidate],
        candidate_history=history,
        current_editor_rows=[],
    )

    assert rows[0]["last_material_change_date"] == ""


def test_description_coverage_uses_raw_description_and_rejection_reason():
    identity_key = "missing-description|alex-example"
    candidate = {
        "identity_key": identity_key,
        "title": "Missing Description",
        "primary_authors": ["Alex Example"],
        "content_description": "",
        "content_description_rejection_reason": "non-string value rejected",
        "source_evidence": [],
    }

    _rows, summary = assemble_cumulative_review(
        current_candidates=[candidate],
        candidate_history=[],
        current_editor_rows=[],
    )

    coverage = summary["description_coverage"]
    assert coverage["validated_count"] == 0
    assert coverage["fallback_count"] == 1
    assert coverage["coverage"] == "partial"
    assert coverage["fallback_ids"] == [identity_key]
    assert coverage["invalid_reason_counts"] == {"non-string value rejected": 1}
