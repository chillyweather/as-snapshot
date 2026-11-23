from __future__ import annotations

import json
from pathlib import Path

import pytest
from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout.exclusion_history import merge_exclusion_records
from autoscout.pipeline import apply_russian_edition_checks
from autoscout.pipeline import apply_update_delta_labels
from autoscout.pipeline import refresh_post_enrichment_artifacts
from autoscout.pipeline import run_pipeline
from autoscout.review_rendering import render_review
from autoscout.review_rendering import russian_verification_review_text
from autoscout.russian_editions import LiveCheckEvidence
from autoscout.russian_editions import apply_live_rsl_checks
from autoscout.russian_editions import is_configured_russian_check
from autoscout.russian_editions import prepare_russian_edition_checks
from autoscout.russian_editions import validate_russian_edition_verification
from autoscout.scouting_candidates import ScoutingCandidate


def _write_config(
    path: Path,
    russian_checks: list[dict[str, object]],
    candidates: list[dict[str, object]] | None = None,
) -> None:
    path.write_text(
        json.dumps(
            {
                "run_date": "2026-08-30",
                "sources": [
                    {
                        "name": "Fixture Discovery",
                        "family": "media/curated list",
                        "url": "https://example.test/discovery",
                        "access_status": "open",
                        "extraction_method": "fixture",
                        "check_frequency": "weekly",
                        "fixture_candidates": candidates
                        or [
                            {
                                "title": "Truthful Check Book",
                                "primary_authors": ["Alex Example"],
                                "source_url": "https://example.test/discovery/truthful-check-book",
                                "signal_strength": "major media list",
                                "evidence_note": "Fixture discovery mention.",
                                "candidate_category": "popular science",
                            }
                        ],
                    }
                ],
                "russian_edition_checks": russian_checks,
            }
        ),
        encoding="utf-8",
    )


def _candidate(output_dir: Path, title: str = "Truthful Check Book") -> dict[str, object]:
    candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
    return next(candidate for candidate in candidates if candidate["title"] == title)


def _checks(output_dir: Path) -> dict[str, object]:
    return json.loads((output_dir / "russian-edition-checks.json").read_text(encoding="utf-8"))


def _positive_verification(url: str, checked_at: str) -> dict[str, object]:
    evidence = {
        "match_type": "same-work Russian-language edition",
        "language": "Russian",
        "url": url,
    }
    return {
        "check_coverage": "complete",
        "provider_outcome": "matched",
        "confidence": "high",
        "checked_sources": ["LitRes"],
        "checked_at": checked_at,
        "query_timestamp": checked_at,
        "evidence_urls": [url],
        "providers": [
            {
                "name": "LitRes",
                "status": "checked",
                "outcome": "matched",
                "evidence_urls": [url],
                "evidence": [evidence],
            }
        ],
        "reason": "",
    }


def test_exclusion_merge_bounds_verified_positive_history_deterministically() -> None:
    older_positive = _positive_verification(
        "https://example.test/ru/older",
        "2026-08-28",
    )
    newer_positive = _positive_verification(
        "https://example.test/ru/newer",
        "2026-08-29",
    )
    incomplete_not_run = {
        "check_coverage": "not_run",
        "provider_outcome": None,
        "confidence": "low",
        "checked_sources": [],
        "checked_at": "2026-08-29",
        "query_timestamp": "2026-08-29",
        "evidence_urls": [],
        "providers": [{"name": "LitRes", "status": "not_run", "outcome": None}],
        "reason": "Provider check was not executed",
    }
    incomplete_partial = {
        "check_coverage": "partial",
        "provider_outcome": None,
        "confidence": "low",
        "checked_sources": ["LitRes"],
        "checked_at": "2026-08-30",
        "query_timestamp": "2026-08-30",
        "evidence_urls": [],
        "providers": [
            {"name": "LitRes", "status": "checked", "outcome": "no_match"},
            {"name": "Book24", "status": "not_run", "outcome": None},
        ],
        "reason": "Book24 was not executed",
    }
    blocked = {
        "check_coverage": "not_run",
        "provider_outcome": "blocked",
        "confidence": "low",
        "checked_sources": [],
        "checked_at": "2026-08-27",
        "query_timestamp": "2026-08-27",
        "evidence_urls": [],
        "providers": [{"name": "LitRes", "status": "blocked", "outcome": "blocked"}],
        "reason": "Provider access is blocked",
    }
    invalid = dict(newer_positive, provider_outcome="not_run")
    records = [
        {
            "identity_key": "bounded history|alex example",
            "exclusion_reason": "found in Russian",
            "exclusion_date": "2026-08-28",
            "russian_edition_verification": older_positive,
        },
        {
            "identity_key": "bounded history|alex example",
            "exclusion_reason": "found in Russian",
            "exclusion_date": "2026-08-29",
            "russian_edition_verification": newer_positive,
        },
        {
            "identity_key": "bounded history|alex example",
            "exclusion_reason": "found in Russian",
            "exclusion_date": "2026-08-29",
            "russian_edition_verification": incomplete_not_run,
            "russian_edition_verification_history": [blocked, invalid],
        },
        {
            "identity_key": "bounded history|alex example",
            "exclusion_reason": "found in Russian",
            "exclusion_date": "2026-08-30",
            "russian_edition_verification": incomplete_partial,
        },
    ]

    merged_results = []
    for ordering in (records, list(reversed(records))):
        merged = merge_exclusion_records(ordering)
        for _ in range(3):
            merged = merge_exclusion_records([*ordering, merged])
        merged_results.append(merged)

    assert merged_results[0] == merged_results[1]
    history = merged_results[0]["russian_edition_verification_history"]
    assert history == [newer_positive]
    assert len(history) == 1


def test_public_run_pipeline_restores_matched_provider_url_not_aggregate_first_url(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(config_path, [])
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    identity_key = "truthful check book|alex example"
    nonmatched_url = "https://example.test/nonmatched-provider/result"
    matched_url = "https://example.test/matched-provider/book"
    excluded_history = [
        {
            "identity_key": identity_key,
            "title": "Truthful Check Book",
            "primary_authors": ["Alex Example"],
            "exclusion_reason": "found in Russian",
            "exclusion_date": "2026-08-29",
            "russian_edition_url": "",
            "evidence": [],
            "russian_edition_verification": {
                "check_coverage": "complete",
                "provider_outcome": "matched",
                "confidence": "high",
                "checked_sources": ["Nonmatched Provider", "Matched Provider"],
                "checked_at": "2026-08-29",
                "query_timestamp": "2026-08-29",
                "evidence_urls": [nonmatched_url, matched_url],
                "providers": [
                    {
                        "name": "Nonmatched Provider",
                        "status": "checked",
                        "outcome": "no_match",
                        "reason": "No candidate match",
                        "evidence_urls": [nonmatched_url],
                        "evidence": [],
                    },
                    {
                        "name": "Matched Provider",
                        "status": "checked",
                        "outcome": "matched",
                        "reason": "",
                        "evidence_urls": [matched_url],
                        "evidence": [
                            {
                                "match_type": "same-work Russian-language edition",
                                "language": "Russian",
                                "url": matched_url,
                            }
                        ],
                    },
                ],
                "reason": "",
            },
        }
    ]
    seed_state(state_dir, excluded_candidate_history=excluded_history)

    run_dir = tmp_path / "run"
    run_pipeline(config_path, run_dir, state_dir=state_dir)

    exclusion = json.loads((run_dir / "internal-exclusions.json").read_text(encoding="utf-8"))[0]
    assert exclusion["exclusion_reason"] == "found in Russian"
    assert exclusion["russian_edition_url"] == matched_url
    assert exclusion["russian_edition_url"] != nonmatched_url
    assert exclusion["evidence"] == [
        {
            "match_type": "same-work Russian-language edition",
            "language": "Russian",
            "url": matched_url,
        }
    ]


def test_public_run_pipeline_does_not_restore_raw_found_exclusion_without_matched_provider(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.json"
    _write_config(config_path, [])
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    raw_url = "https://example.test/raw-positive-evidence"
    raw_evidence = {
        "match_type": "same-work Russian-language edition",
        "language": "Russian",
        "url": raw_url,
    }
    seed_state(
        state_dir,
        excluded_candidate_history=[
            {
                "identity_key": "truthful check book|alex example",
                "title": "Truthful Check Book",
                "primary_authors": ["Alex Example"],
                "exclusion_reason": "found in Russian",
                "exclusion_date": "2026-08-29",
                "russian_edition_url": raw_url,
                "evidence_urls": [raw_url],
                "evidence": [raw_evidence],
                "russian_edition_evidence": [raw_evidence],
                "russian_edition_verification": {
                    "check_coverage": "complete",
                    "provider_outcome": "matched",
                    "checked_sources": ["Unverified Provider"],
                    "checked_at": "2026-08-29",
                    "query_timestamp": "2026-08-29",
                    "evidence_urls": [raw_url],
                    "providers": [
                        {
                            "name": "Unverified Provider",
                            "status": "checked",
                            "outcome": "matched",
                            "evidence_urls": [raw_url],
                            "evidence": [raw_evidence],
                        }
                    ],
                    "reason": "",
                },
            }
        ],
    )

    run_dir = tmp_path / "run"
    run_pipeline(config_path, run_dir, state_dir=state_dir)

    candidate = _candidate(run_dir)
    assert candidate.get("russian_edition_status", "") != "found in Russian"
    assert candidate.get("russian_edition_url", "") != raw_url
    assert candidate.get("russian_edition_evidence", []) != [raw_evidence]
    assert "russian_edition_verification" not in candidate


def test_public_run_pipeline_verifies_same_work_russian_direct_url(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [
            {
                "retailer": "LitRes",
                "url": "https://www.litres.ru",
                "fixture_results": [
                    {
                        "candidate_title": "Truthful Check Book",
                        "primary_authors": ["Alex Example"],
                        "match_type": "same-work Russian-language edition",
                        "language": "Russian",
                        "edition_url": "https://www.litres.ru/book/truthful-check-book-1/",
                    }
                ],
            }
        ],
    )

    run_pipeline(config_path, tmp_path / "run")

    exclusion = json.loads((tmp_path / "run" / "internal-exclusions.json").read_text(encoding="utf-8"))[0]
    verification = exclusion["russian_edition_verification"]
    assert exclusion["exclusion_reason"] == "found in Russian"
    assert verification["check_coverage"] == "complete"
    assert verification["provider_outcome"] == "matched"
    assert verification["checked_sources"] == ["LitRes"]
    assert verification["checked_at"] == "2026-08-30"
    assert verification["evidence_urls"] == ["https://www.litres.ru/book/truthful-check-book-1/"]


def test_apply_live_rsl_checks_returns_structured_per_candidate_evidence(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    output_dir = tmp_path / "run"
    identity_key = "truthful check book|alex example"
    candidate = {
        "identity_key": identity_key,
        "title": "Truthful Check Book",
        "primary_authors": ["Alex Example"],
    }
    direct_url = "https://www.litres.ru/book/truthful-check-book-1/"
    checks = [
        {
            "retailer": "LitRes",
            "url": "https://www.litres.ru",
            "enable_live": True,
            "fixture_live_results": [
                {
                    "query": "Truthful Check Book",
                    "result_url": direct_url,
                    "result_title": "Truthful Check Book",
                    "candidate_title": "Truthful Check Book",
                    "primary_authors": ["Alex Example"],
                    "match_type": "same-work Russian-language edition",
                    "language": "Russian",
                    "check_date": "2026-08-30",
                }
            ],
        }
    ]
    prepared = prepare_russian_edition_checks(config_path, output_dir, checks, "2026-08-30")
    prepared_check = prepared[0]
    live_evidence = apply_live_rsl_checks([candidate], prepared, "2026-08-30", config_path, output_dir)

    assert isinstance(live_evidence, tuple)
    assert len(live_evidence) == 1
    provider_evidence = live_evidence[0]
    assert isinstance(provider_evidence, LiveCheckEvidence)
    assert provider_evidence.provider_name == "LitRes"
    candidate_evidence = provider_evidence.for_candidate(identity_key)
    assert candidate_evidence.status == "checked"
    assert len(candidate_evidence.results) == 1
    assert candidate_evidence.results[0]["edition_url"] == direct_url
    for private_key in ("_live_candidate_results", "_live_candidate_errors", "_live_candidate_executed"):
        assert private_key not in prepared_check


@pytest.mark.parametrize(
    ("current_check", "expected_outcome", "expected_review_text"),
    [
        ({"retailer": "LitRes", "fixture_results": []}, "not_run", "проверка не запускалась"),
        (
            {"retailer": "LitRes", "access_status": "blocked", "fixture_results": []},
            "blocked",
            "доступ к провайдеру заблокирован",
        ),
    ],
)
def test_public_run_pipeline_preserves_verified_positive_when_provider_is_non_definitive(
    tmp_path: Path, current_check: dict[str, object], expected_outcome: str, expected_review_text: str
) -> None:
    first_config_path = tmp_path / "first-config.json"
    _write_config(
        first_config_path,
        [
            {
                "retailer": "LitRes",
                "fixture_results": [
                    {
                        "candidate_title": "Truthful Check Book",
                        "primary_authors": ["Alex Example"],
                        "match_type": "same-work Russian-language edition",
                        "language": "Russian",
                        "edition_url": "https://www.litres.ru/book/truthful-check-book-1/",
                    }
                ],
            }
        ],
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    identity_key = "truthful check book|alex example"
    initial_history = [
        {
            "identity_key": identity_key,
            "candidate_id": "candidate-1",
            "editor_status": "maybe_watch",
            "editor_note": "Keep this note.",
        }
    ]
    seed_state(state_dir, candidate_history=initial_history)
    visibility = [{"identity_key": identity_key, "hidden_by_rule_id": "hard-rule-7"}]
    seed_state(state_dir, candidate_visibility=visibility)
    first_output_dir = tmp_path / "first-run"
    run_pipeline(first_config_path, first_output_dir, state_dir=state_dir)
    first_summary = json.loads((first_output_dir / "run-summary.json").read_text(encoding="utf-8"))
    assert first_summary["russian_verification_breakdown"]["verified_russian_edition"] == 1

    first_exclusions = read_state(state_dir, "excluded_candidate_history")
    first_record = first_exclusions[0]
    first_record["russian_edition_note"] = "Previously confirmed Russian edition."
    seed_state(state_dir, excluded_candidate_history=first_exclusions)
    visibility_before = state_snapshot(state_dir)["candidate_visibility"]
    feedback_before = {field: initial_history[0][field] for field in ("editor_status", "editor_note")}
    positive_before = {
        "russian_edition_status": "found in Russian",
        "russian_edition_url": first_record["russian_edition_url"],
        "russian_edition_evidence": first_record["evidence"],
        "russian_edition_note": first_record["russian_edition_note"],
    }

    blocked_config_path = tmp_path / "blocked-config.json"
    _write_config(blocked_config_path, [current_check])
    second_output_dir = tmp_path / "blocked-run"
    run_pipeline(blocked_config_path, second_output_dir, state_dir=state_dir)

    assert json.loads((second_output_dir / "scouting-candidates.json").read_text(encoding="utf-8")) == []
    exclusion = json.loads((second_output_dir / "internal-exclusions.json").read_text(encoding="utf-8"))[0]
    assert exclusion["exclusion_reason"] == "found in Russian"
    assert exclusion["russian_edition_url"] == positive_before["russian_edition_url"]
    assert exclusion["evidence"] == positive_before["russian_edition_evidence"]
    assert exclusion["russian_edition_note"] == positive_before["russian_edition_note"]

    second_history = read_state(state_dir, "candidate_history")
    second_record = second_history[0]
    assert {field: second_record.get(field, "") for field in feedback_before} == feedback_before
    verification = exclusion["russian_edition_verification"]
    assert verification["check_coverage"] == "not_run"
    expected_aggregate = None if expected_outcome == "not_run" else expected_outcome
    expected_status = "not_run" if expected_outcome == "not_run" else expected_outcome
    assert verification["provider_outcome"] == expected_aggregate
    assert verification["providers"][0]["status"] == expected_status
    assert verification["providers"][0]["outcome"] == expected_aggregate
    assert render_review(second_output_dir, state_dir=state_dir) == 0
    markdown = (second_output_dir / "weekly-review.md").read_text(encoding="utf-8")
    html = (second_output_dir / "weekly-review.html").read_text(encoding="utf-8")
    assert "Есть русское издание" in markdown
    assert positive_before["russian_edition_url"] in markdown
    assert expected_review_text in markdown
    assert 'data-key="russian"' not in html
    assert "<dt>Проверка русского издания</dt>" not in html
    assert "<dt>Русское издание</dt>" not in html
    assert state_snapshot(state_dir)["candidate_visibility"] == visibility_before


def test_public_run_pipeline_accepts_explicit_complete_no_match_fixture(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [{"retailer": "LitRes", "fixture_results": [{"status": "no_match", "candidate_title": "Truthful Check Book"}]}],
    )

    run_pipeline(config_path, tmp_path / "run")

    candidate = _candidate(tmp_path / "run")
    assert candidate["russian_edition_status"] == "not found in Russian"
    assert candidate["russian_edition_verification"]["check_coverage"] == "complete"
    assert candidate["russian_edition_verification"]["provider_outcome"] == "no_match"
    assert candidate["russian_edition_verification"]["providers"][0]["status"] == "checked"
    assert candidate["russian_edition_verification"]["providers"][0]["outcome"] == "no_match"
    artifact = _checks(tmp_path / "run")
    decision_reason = artifact["candidate_checks"][0]["decision_reason"]
    assert decision_reason == "configured Russian providers completed successfully with no credible match"
    assert decision_reason.isascii()


def test_public_run_pipeline_uses_only_matched_provider_evidence_for_positive_aggregation(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [
            {
                "retailer": "LitRes",
                "fixture_results": [
                    {
                        "candidate_title": "Truthful Check Book",
                        "primary_authors": ["Alex Example"],
                        "match_type": "same-work Russian-language edition",
                        "language": "Russian",
                        "edition_url": "https://www.litres.ru/book/truthful-check-book-1/",
                    }
                ],
            },
            {
                "retailer": "Ozon",
                "fixture_results": [{"status": "no_match", "candidate_title": "Truthful Check Book"}],
            },
        ],
    )

    run_pipeline(config_path, tmp_path / "run")

    exclusion = json.loads((tmp_path / "run" / "internal-exclusions.json").read_text(encoding="utf-8"))[0]
    assert exclusion["exclusion_reason"] == "found in Russian"
    assert exclusion["russian_edition_url"] == "https://www.litres.ru/book/truthful-check-book-1/"
    assert exclusion["evidence"]
    assert {item["retailer"] for item in exclusion["evidence"]} == {"LitRes"}


def test_public_run_pipeline_does_not_bind_unscoped_no_match_to_one_candidate(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(config_path, [{"retailer": "LitRes", "fixture_results": [{"status": "no_match"}]}])

    run_pipeline(config_path, tmp_path / "run")

    candidate = _candidate(tmp_path / "run")
    assert candidate["russian_edition_status"] == "unclear"
    assert candidate["russian_edition_verification"]["provider_outcome"] is None
    assert candidate["russian_edition_verification"]["providers"][0]["status"] == "not_run"
    assert candidate["russian_edition_verification"]["providers"][0]["outcome"] is None


def test_validate_russian_edition_verification_rejects_not_run_provider_outcome() -> None:
    verification = {
        "check_coverage": "not_run",
        "provider_outcome": None,
        "confidence": "low",
        "checked_sources": [],
        "checked_at": "2026-08-30",
        "query_timestamp": "2026-08-30",
        "evidence_urls": [],
        "providers": [{"name": "LitRes", "status": "not_run", "outcome": None}],
        "reason": "",
    }
    validate_russian_edition_verification(verification)

    verification["provider_outcome"] = "not_run"
    with pytest.raises(ValueError, match="provider_outcome"):
        validate_russian_edition_verification(verification)


def test_empty_russian_check_is_not_required_but_named_unknown_provider_is_required() -> None:
    assert is_configured_russian_check({}) is False
    assert is_configured_russian_check({"retailer": "Unknown Russian Retailer"}) is True
    assert is_configured_russian_check({"family": "publisher-site"}) is False


def test_public_run_pipeline_keeps_unknown_declared_provider_incomplete(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [
            {
                "retailer": "Unknown Russian Retailer",
                "fixture_results": [{"status": "no_match", "candidate_title": "Truthful Check Book"}],
            }
        ],
    )

    run_pipeline(config_path, tmp_path / "run")

    candidate = _candidate(tmp_path / "run")
    verification = candidate["russian_edition_verification"]
    assert candidate["russian_edition_status"] == "unclear"
    assert verification["check_coverage"] == "not_run"
    assert verification["provider_outcome"] == "unimplemented"
    assert verification["providers"][0]["status"] == "unimplemented"
    assert _checks(tmp_path / "run")["retailer_runs"][0]["status"] == "unimplemented"


def test_public_run_pipeline_does_not_turn_empty_fixture_into_negative(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(config_path, [{"retailer": "Ozon", "fixture_results": []}])

    run_pipeline(config_path, tmp_path / "run")

    candidate = _candidate(tmp_path / "run")
    assert candidate["russian_edition_status"] == "unclear"
    assert candidate["russian_edition_verification"]["check_coverage"] == "not_run"
    assert candidate["russian_edition_verification"]["provider_outcome"] is None
    assert candidate["russian_edition_verification"]["providers"][0]["status"] == "not_run"
    assert candidate["russian_edition_verification"]["providers"][0]["outcome"] is None
    assert _checks(tmp_path / "run")["retailer_runs"][0]["status"] == "not_run"


def test_public_run_pipeline_does_not_infer_sibling_no_match_from_one_fixture_record(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    candidates = [
        {
            "title": "Truthful Check Book",
            "primary_authors": ["Alex Example"],
            "source_url": "https://example.test/discovery/truthful-check-book",
            "signal_strength": "major media list",
            "evidence_note": "Fixture discovery mention.",
        },
        {
            "title": "Sibling Evidence Book",
            "primary_authors": ["Sam Example"],
            "source_url": "https://example.test/discovery/sibling-evidence-book",
            "signal_strength": "major media list",
            "evidence_note": "Fixture discovery mention.",
        },
    ]
    _write_config(
        config_path,
        [{"retailer": "LitRes", "fixture_results": [{"status": "no_match", "candidate_title": "Truthful Check Book"}]}],
        candidates,
    )

    run_pipeline(config_path, tmp_path / "run")

    assert _candidate(tmp_path / "run")["russian_edition_status"] == "not found in Russian"
    sibling = _candidate(tmp_path / "run", "Sibling Evidence Book")
    assert sibling["russian_edition_status"] == "unclear"
    assert sibling["russian_edition_verification"]["check_coverage"] == "not_run"


def test_public_run_pipeline_aggregates_mixed_no_match_and_not_run_as_not_run(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [
            {
                "retailer": "LitRes",
                "fixture_results": [{"status": "no_match", "candidate_title": "Truthful Check Book"}],
            },
            {"retailer": "Ozon", "fixture_results": []},
        ],
    )

    run_pipeline(config_path, tmp_path / "run")

    verification = _candidate(tmp_path / "run")["russian_edition_verification"]
    assert verification["check_coverage"] == "partial"
    assert verification["provider_outcome"] is None
    assert [provider["status"] for provider in verification["providers"]] == ["checked", "not_run"]
    assert [provider["outcome"] for provider in verification["providers"]] == ["no_match", None]
    assert _candidate(tmp_path / "run")["russian_edition_status"] == "unclear"
    summary = json.loads((tmp_path / "run" / "run-summary.json").read_text(encoding="utf-8"))
    assert summary["russian_verification_breakdown"]["partial"] == 1
    assert summary["russian_verification_breakdown"]["not_run"] == 0


def test_public_run_pipeline_records_disabled_provider_without_negative(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [
            {
                "retailer": "LitRes",
                "active": False,
                "fixture_results": [{"status": "no_match", "candidate_title": "Truthful Check Book"}],
            }
        ],
    )

    run_pipeline(config_path, tmp_path / "run")

    candidate = _candidate(tmp_path / "run")
    verification = candidate["russian_edition_verification"]
    assert candidate["russian_edition_status"] == "unclear"
    assert verification["check_coverage"] == "not_run"
    assert verification["providers"][0]["status"] == "skipped"
    assert verification["providers"][0]["outcome"] is None
    assert "disabled" in verification["providers"][0]["reason"]
    assert _checks(tmp_path / "run")["retailer_runs"][0]["status"] == "skipped"


def test_public_run_pipeline_records_unimplemented_provider(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(config_path, [{"retailer": "Ozon", "enable_live": True}])

    run_pipeline(config_path, tmp_path / "run")

    verification = _candidate(tmp_path / "run")["russian_edition_verification"]
    assert verification["check_coverage"] == "not_run"
    assert verification["provider_outcome"] == "unimplemented"
    assert verification["providers"][0]["status"] == "unimplemented"
    assert _checks(tmp_path / "run")["retailer_runs"][0]["status"] == "unimplemented"


def test_public_run_pipeline_records_blocked_provider(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(config_path, [{"retailer": "LitRes", "access_status": "blocked", "fixture_results": []}])

    run_pipeline(config_path, tmp_path / "run")

    verification = _candidate(tmp_path / "run")["russian_edition_verification"]
    assert verification["check_coverage"] == "not_run"
    assert verification["provider_outcome"] == "blocked"
    assert verification["providers"][0]["status"] == "blocked"
    assert _checks(tmp_path / "run")["retailer_runs"][0]["status"] == "blocked"
    summary = json.loads((tmp_path / "run" / "run-summary.json").read_text(encoding="utf-8"))
    assert summary["russian_check_blocked_count"] == 1


def test_public_run_pipeline_continues_after_one_provider_failure(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [
            {"retailer": "LitRes", "enable_live": True, "fixture_live_results": [{"status": "timeout"}]},
            {
                "retailer": "Book24",
                "fixture_results": [{"status": "no_match", "candidate_title": "Truthful Check Book"}],
            },
        ],
    )

    run_pipeline(config_path, tmp_path / "run")

    providers = _candidate(tmp_path / "run")["russian_edition_verification"]["providers"]
    assert [provider["status"] for provider in providers] == ["failed", "checked"]
    assert [provider["outcome"] for provider in providers] == ["failed", "no_match"]
    assert _candidate(tmp_path / "run")["russian_edition_status"] == "unclear"
    assert render_review(tmp_path / "run") == 0
    markdown = (tmp_path / "run" / "weekly-review.md").read_text(encoding="utf-8")
    html = (tmp_path / "run" / "weekly-review.html").read_text(encoding="utf-8")
    assert "тайм-аут при обращении к провайдеру" in markdown
    assert "тайм-аут при обращении к провайдеру" not in html
    for review in (markdown, html):
        assert "Provider request timed out" not in review


def test_public_run_pipeline_keeps_ambiguous_identity_unclear(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [
            {
                "retailer": "LitRes",
                "fixture_results": [
                    {
                        "candidate_title": "Truthful Check Book",
                        "primary_authors": ["Alex Example"],
                        "match_type": "ambiguous author/title match",
                        "language": "Russian",
                        "edition_url": "https://www.litres.ru/book/ambiguous/",
                    }
                ],
            }
        ],
    )

    run_pipeline(config_path, tmp_path / "run")

    verification = _candidate(tmp_path / "run")["russian_edition_verification"]
    assert _candidate(tmp_path / "run")["russian_edition_status"] == "unclear"
    assert verification["provider_outcome"] == "ambiguous"
    assert verification["providers"][0]["status"] == "checked"
    assert verification["providers"][0]["outcome"] == "ambiguous"


def test_public_run_pipeline_marks_non_russian_same_work_clue_ambiguous(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [
            {
                "retailer": "LitRes",
                "fixture_results": [
                    {
                        "candidate_title": "Truthful Check Book",
                        "primary_authors": ["Alex Example"],
                        "match_type": "same-work Russian-language edition",
                        "language": "English",
                        "edition_url": "https://www.litres.ru/book/imported/",
                    }
                ],
            }
        ],
    )

    run_pipeline(config_path, tmp_path / "run")

    candidate = _candidate(tmp_path / "run")
    verification = candidate["russian_edition_verification"]
    assert candidate["russian_edition_status"] == "unclear"
    assert verification["provider_outcome"] == "ambiguous"
    assert verification["providers"][0]["status"] == "checked"
    assert verification["providers"][0]["outcome"] == "ambiguous"


def test_public_run_pipeline_rejects_foreign_language_as_russian_match(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [
            {
                "name": "RSL",
                "family": "russian-library-catalog",
                "fixture_results": [
                    {
                        "candidate_title": "Truthful Check Book",
                        "primary_authors": ["Alex Example"],
                        "match_type": "imported foreign-language listing",
                        "language_clue": "English",
                        "record_url": "https://search.rsl.ru/record/foreign",
                    }
                ],
            }
        ],
    )

    run_pipeline(config_path, tmp_path / "run")

    candidate = _candidate(tmp_path / "run")
    assert candidate["russian_edition_status"] == "not found in Russian"
    assert candidate["russian_edition_verification"]["provider_outcome"] == "no_match"
    assert candidate["russian_edition_verification"]["providers"][0]["status"] == "checked"
    assert candidate["russian_edition_verification"]["providers"][0]["outcome"] == "no_match"


def test_public_run_pipeline_reports_verification_in_summary_and_reviews(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(config_path, [{"retailer": "Ozon", "fixture_results": []}])
    output_dir = tmp_path / "run"

    run_pipeline(config_path, output_dir)
    assert render_review(output_dir) == 0

    summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
    assert summary["russian_verification_breakdown"]["not_run"] == 1
    artifact = _checks(output_dir)
    assert artifact["verification_breakdown"]["not_run"] == 1
    assert artifact["candidate_checks"][0]["russian_edition_verification"]["providers"][0]["name"] == "Ozon"

    markdown = (output_dir / "weekly-review.md").read_text(encoding="utf-8")
    html = (output_dir / "weekly-review.html").read_text(encoding="utf-8")
    assert "Проверка русского издания" in markdown
    assert "не запускалась" in markdown
    assert "Ozon" in markdown
    assert "Проверка русского издания" not in html
    assert "не запускалась" not in html
    for review in (markdown, html):
        assert "Русское издание не найдено" not in review


def test_public_run_pipeline_russian_verification_replay_is_deterministic(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [{"retailer": "LitRes", "fixture_results": [{"status": "no_match", "candidate_title": "Truthful Check Book"}]}],
    )

    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    run_pipeline(config_path, first_dir)
    run_pipeline(config_path, second_dir)

    for artifact in ("scouting-candidates.json", "russian-edition-checks.json", "run-summary.json"):
        assert (first_dir / artifact).read_bytes() == (second_dir / artifact).read_bytes()


def test_legacy_negative_without_recognized_provider_becomes_auditable_not_run_state() -> None:
    positive_evidence = {
        "match_type": "same-work Russian-language edition",
        "language": "Russian",
        "url": "https://example.test/ru/positive-evidence",
    }
    candidate = {
        "identity_key": "legacy book|legacy author",
        "title": "Legacy Book",
        "primary_authors": ["Legacy Author"],
        "russian_edition_status": "not found in Russian",
        "russian_edition_evidence": [positive_evidence],
        "editor_status": "maybe_watch",
        "editor_note": "Keep this editorial note verbatim.",
    }
    apply_russian_edition_checks([candidate], [], "2026-08-30")

    assert candidate["russian_edition_status"] == "unclear"
    assert candidate["russian_edition_verification"] == {
        "check_coverage": "not_run",
        "provider_outcome": None,
        "confidence": "low",
        "checked_sources": [],
        "checked_at": "2026-08-30",
        "query_timestamp": "2026-08-30",
        "evidence_urls": [],
        "providers": [],
        "reason": "No recognized Russian-edition providers were configured",
    }
    assert candidate["russian_edition_evidence"] == [positive_evidence]
    assert candidate["editor_status"] == "maybe_watch"
    assert candidate["editor_note"] == "Keep this editorial note verbatim."


def test_pipeline_normalizes_legacy_negative_history_without_touching_visibility(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(config_path, [])
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    identity_key = "truthful check book|alex example"
    seed_state(
        state_dir,
        candidate_history=[
            {
                "identity_key": identity_key,
                "candidate_id": "candidate-1",
                "russian_edition_status": "not found in Russian",
                "editor_status": "maybe_watch",
                "editor_note": "Keep this note.",
            }
        ],
    )
    visibility = [{"identity_key": identity_key, "hidden_by_rule_id": "hard-rule-7"}]
    seed_state(state_dir, candidate_visibility=visibility)

    run_pipeline(config_path, tmp_path / "run", state_dir=state_dir)

    candidate = _candidate(tmp_path / "run")
    assert candidate["russian_edition_status"] == "unclear"
    assert candidate["russian_edition_verification"]["provider_outcome"] is None
    summary = json.loads((tmp_path / "run" / "run-summary.json").read_text(encoding="utf-8"))
    assert summary["russian_verification_breakdown"]["not_run"] == 1
    artifact = _checks(tmp_path / "run")
    assert artifact["verification_breakdown"]["not_run"] == 1
    assert artifact["candidate_checks"][0]["russian_edition_verification"]["provider_outcome"] is None

    history = read_state(state_dir, "candidate_history")
    assert history[0]["russian_edition_status"] == "unclear"
    assert history[0]["russian_edition_verification"]["provider_outcome"] is None
    assert history[0]["editor_status"] == "maybe_watch"
    assert history[0]["editor_note"] == "Keep this note."
    assert read_state(state_dir, "candidate_visibility") == visibility


def test_post_enrichment_refresh_keeps_russian_summary_and_artifact_in_sync(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(config_path, [{"retailer": "Ozon", "fixture_results": []}])
    output_dir = tmp_path / "run"
    run_pipeline(config_path, output_dir)

    (output_dir / "scouting-candidates.json").write_text("[]", encoding="utf-8")
    assert refresh_post_enrichment_artifacts(config_path, output_dir)["status"] == "ok"

    summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
    artifact = _checks(output_dir)
    assert summary["russian_verification_breakdown"] == artifact["verification_breakdown"]
    assert summary["russian_verification_breakdown"]["not_run"] == 0
    assert artifact["candidate_checks"] == []


def test_russian_review_reason_mapping_hides_raw_timeout_details() -> None:
    text = russian_verification_review_text(
        {
            "check_coverage": "failed",
            "provider_outcome": "failed",
            "providers": [
                {
                    "name": "LitRes",
                    "status": "failed",
                    "reason": "Provider request timed out while reading /private/fixture.json",
                }
            ],
        }
    )

    assert "тайм-аут при обращении к провайдеру" in text
    assert "/private/fixture.json" not in text
    assert "Provider request timed out" not in text


def test_legacy_negative_normalization_is_not_a_material_change() -> None:
    candidate = ScoutingCandidate(
        {
            "identity_key": "book|author",
            "russian_edition_status": "unclear",
            "russian_edition_verification": {
                "check_coverage": "not_run",
                "provider_outcome": None,
            },
        }
    )
    history = {"identity_key": "book|author", "russian_edition_status": "not found in Russian"}

    assert candidate.material_change_reasons(history) == []
    apply_update_delta_labels([candidate], [history])
    assert candidate.data["update_delta"] == "unchanged"


def test_definitive_russian_status_change_remains_material() -> None:
    candidate = ScoutingCandidate(
        {
            "russian_edition_status": "unclear",
            "russian_edition_verification": {
                "check_coverage": "complete",
                "provider_outcome": "ambiguous",
            },
        }
    )

    assert candidate.material_change_reasons({"russian_edition_status": "not found in Russian"}) == [
        "Russian status changed"
    ]


def test_exclusion_without_verification_keeps_legacy_shape() -> None:
    record = ScoutingCandidate({"identity_key": "book|author"}).exclusion_record("ineligible", "2026-08-30")
    assert "russian_edition_verification" not in record

    record = ScoutingCandidate(
        {
            "identity_key": "book|author",
            "russian_edition_verification": {"provider_outcome": "not_run"},
        }
    ).exclusion_record("ineligible", "2026-08-30")
    assert "russian_edition_verification" not in record


def test_public_run_pipeline_preserves_editorial_state_while_adding_verification(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [{"retailer": "LitRes", "fixture_results": [{"status": "no_match", "candidate_title": "Truthful Check Book"}]}],
    )
    state_dir = tmp_path / "state"
    identity_key = "truthful check book|alex example"
    history = [
        {
            "identity_key": identity_key,
            "candidate_id": "candidate-1",
            "editor_status": "maybe_watch",
            "editor_note": "Verbatim editor wording",
        }
    ]
    visibility = [{"identity_key": identity_key, "hidden_by_rule_id": "hard-rule-7"}]
    state_dir.mkdir()
    seed_state(state_dir, candidate_history=history)
    seed_state(state_dir, candidate_visibility=visibility)

    run_pipeline(config_path, tmp_path / "run", state_dir=state_dir)

    saved_history = read_state(state_dir, "candidate_history")
    saved_visibility = read_state(state_dir, "candidate_visibility")
    assert saved_history[0]["editor_status"] == "maybe_watch"
    assert saved_history[0]["editor_note"] == "Verbatim editor wording"
    assert saved_visibility == visibility
    assert _candidate(tmp_path / "run")["russian_edition_verification"]["provider_outcome"] == "no_match"
