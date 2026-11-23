"""Offline tests for issue #589, undated recurring ranking freshness.

The markup is synthetic first-party-shaped HTML.  It is not live evidence and
it does not claim that any source has passed a live probe or an onboarding
confirmation gate.  No test here makes a network request.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from state_store_support import read_state

from autoscout.ranking_freshness import RankingFreshnessLedger
from autoscout.ranking_freshness import freshness_status_is_skip
from autoscout.ranking_freshness import ranking_freshness_state_key
from autoscout.ranking_freshness import ranking_row_fingerprint
from autoscout.ranking_freshness import ranking_undated_declared
from autoscout.ranking_freshness import undated_freshness_failure
from autoscout.source_collection import SourceCollection
from autoscout.source_collectors import CollectorRunError
from autoscout.source_onboarding_transaction import default_replay_runner
from autoscout.source_page_identity import ranking_rows
from autoscout.source_page_identity import validate_ranking_contract_metadata
from autoscout.source_problem_notes import editor_source_reason
from autoscout.state import load_ranking_freshness
from autoscout.state import write_ranking_freshness
from autoscout.state_store import Collection
from autoscout.state_store.json_backend import FILE_NAMES

STATE_RANKING_FRESHNESS = FILE_NAMES[Collection.RANKING_FRESHNESS]

URL = "https://example.test/100-mas-vendidos/"
HEADING = "Los 100 más vendidos de la semana"
SOURCE_NAME = "Synthetic undated ranking"

UNDATED_METADATA: dict[str, Any] = {
    "contract_family": "ranking-table",
    "metadata_ranking_table": True,
    "ranking_row_selector": ".ranked-row",
    "ranking_rank_source": "document_order",
    "ranking_title_selector": ".row-title",
    "ranking_author_selector": ".row-author",
    "ranking_identifier_selector": ".row-isbn",
    "ranking_category_marker": "más vendidos",
    "ranking_reporting_period_undated": True,
    "ranking_expected_raw_row_count": 2,
    "ranking_evidence_url": "source_page",
    "adult_nonfiction_exclusions": ["libro de actividades"],
    "visible_title_selector": ".page-heading",
    "expected_page_heading": HEADING,
    "expected_signal_strength": "top ranking",
    "eligible_candidate_category": "adult nonfiction",
    "language": "es",
}


def _row(title: str, author: str, isbn: str) -> str:
    return (
        '<div class="ranked-row">'
        f'<div class="row-title">{title}</div>'
        f'<div class="row-author">{author}</div>'
        f'<span class="row-isbn" hidden>{isbn}</span>'
        "</div>"
    )


def _html(rows: str) -> str:
    return (
        "<!doctype html><html><head><title>Stale title</title></head><body>"
        f'<div class="page-heading">{HEADING}</div>{rows}</body></html>'
    )


_FIRST_LIST = _row("HISTORIA ARGENTINA", "AUTORA UNA", "9780306406157") + _row(
    "CIENCIA POLITICA", "AUTOR DOS", "9780140328721"
)
_MOVED_LIST = _row("CIENCIA POLITICA", "AUTOR DOS", "9780140328721") + _row(
    "HISTORIA ARGENTINA", "AUTORA UNA", "9780306406157"
)
# Movement that keeps rank 1 intact, so the retained extraction response stays
# grounded and only the freshness verdict is under test.
_CHANGED_LIST = _row("HISTORIA ARGENTINA", "AUTORA UNA", "9780306406157") + _row(
    "TERCERA OBRA", "AUTORA TRES", "9781401971366"
)


def _source(**overrides: Any) -> dict[str, Any]:
    return {
        "name": SOURCE_NAME,
        "family": "bestseller/ranking",
        "url": URL,
        **UNDATED_METADATA,
        **overrides,
    }


def _fingerprint(html: str, source: dict[str, Any]) -> str:
    return ranking_row_fingerprint(ranking_rows(html, source))


# --- Declaration -----------------------------------------------------------


def test_undated_declaration_replaces_the_dated_reporting_trio() -> None:
    assert validate_ranking_contract_metadata(_source()) == ""
    assert ranking_undated_declared(_source()) is True


def test_dated_ranking_still_requires_its_reporting_period_pattern() -> None:
    source = _source()
    source.pop("ranking_reporting_period_undated")
    assert validate_ranking_contract_metadata(source) == "ranking-table requires ranking_reporting_period_pattern"


def test_undated_declaration_cannot_be_combined_with_dated_metadata() -> None:
    source = _source(ranking_reporting_period_pattern=r"(?P<period>\d{4})")
    error = validate_ranking_contract_metadata(source)
    assert "cannot be combined with dated reporting metadata" in error
    assert "ranking_reporting_period_pattern" in error


def test_undated_declaration_rejects_a_declared_maximum_age() -> None:
    error = validate_ranking_contract_metadata(_source(ranking_reporting_period_max_age_days=7))
    assert "ranking_reporting_period_max_age_days" in error


def test_undated_declaration_must_be_a_boolean() -> None:
    error = validate_ranking_contract_metadata(_source(ranking_reporting_period_undated="yes"))
    assert error == "ranking_reporting_period_undated must be true or false"


def test_a_declared_false_undated_flag_keeps_the_dated_contract() -> None:
    source = _source(ranking_reporting_period_undated=False)
    assert validate_ranking_contract_metadata(source) == "ranking-table requires ranking_reporting_period_pattern"


# --- Fingerprint -----------------------------------------------------------


def test_fingerprint_is_deterministic_for_identical_rows() -> None:
    source = _source()
    assert _fingerprint(_html(_FIRST_LIST), source) == _fingerprint(_html(_FIRST_LIST), source)


def test_fingerprint_covers_row_order_so_a_reordered_list_is_movement() -> None:
    source = _source()
    assert _fingerprint(_html(_FIRST_LIST), source) != _fingerprint(_html(_MOVED_LIST), source)


def test_fingerprint_covers_the_whole_bounded_set_not_a_sample() -> None:
    source = _source(ranking_expected_raw_row_count=3)
    extended = _FIRST_LIST + _row("TERCERA OBRA", "AUTORA TRES", "9781401971366")
    assert _fingerprint(_html(_FIRST_LIST), source) != _fingerprint(_html(extended), source)


# --- Verdict ---------------------------------------------------------------


def test_first_observation_without_a_prior_record_is_allowed() -> None:
    reason, status = undated_freshness_failure(_source(), "abc", RankingFreshnessLedger())
    assert reason == ""
    assert status == ""


def test_missing_state_fails_closed() -> None:
    reason, status = undated_freshness_failure(_source(), "abc", None)
    assert "requires retained freshness state" in reason
    assert status == "missing_undated_freshness_state"


def test_missing_fingerprint_fails_closed() -> None:
    reason, status = undated_freshness_failure(_source(), "", RankingFreshnessLedger())
    assert "produced no bounded row fingerprint" in reason
    assert status == "missing_undated_freshness_state"


def test_unchanged_fingerprint_is_reported_as_frozen() -> None:
    source = _source()
    ledger = RankingFreshnessLedger().observe(source, "abc", "2026-08-17")
    reason, status = undated_freshness_failure(source, "abc", ledger)
    assert reason == "undated ranking list is unchanged since 2026-08-17 and cannot be reported as current"
    assert status == "ranking_list_frozen"


@pytest.mark.parametrize(
    ("cadence", "run_date", "status"),
    [
        # 2026-11-03: a weekly list last changed the day before was failed as frozen.
        ({"check_frequency": "weekly"}, "2026-08-18", "ranking_list_not_yet_refreshed"),
        ({"check_frequency": "weekly"}, "2026-08-17", "ranking_list_not_yet_refreshed"),
        ({"check_frequency": "weekly"}, "2026-08-23", "ranking_list_not_yet_refreshed"),
        ({"check_frequency": "weekly"}, "2026-08-24", "ranking_list_frozen"),
        ({"refresh_cadence": "Weekly"}, "2026-08-20", "ranking_list_not_yet_refreshed"),
        ({"check_frequency": "monthly"}, "2026-09-10", "ranking_list_not_yet_refreshed"),
        ({"check_frequency": "monthly"}, "2026-09-17", "ranking_list_frozen"),
        # Without a known cadence or a run date the strict rule holds.
        ({"check_frequency": "whenever"}, "2026-08-18", "ranking_list_frozen"),
        ({"check_frequency": "weekly"}, "", "ranking_list_frozen"),
        # A run dated before the last change proves nothing about the cadence.
        ({"check_frequency": "weekly"}, "2026-08-10", "ranking_list_frozen"),
    ],
)
def test_an_unchanged_list_is_judged_against_its_cadence(cadence, run_date, status) -> None:
    source = {**_source(), "check_frequency": "", **cadence}
    ledger = RankingFreshnessLedger().observe(source, "abc", "2026-08-17")
    reason, verdict = undated_freshness_failure(source, "abc", ledger, run_date)
    assert verdict == status
    assert reason
    assert freshness_status_is_skip(verdict) is (status == "ranking_list_not_yet_refreshed")


def test_a_list_skipped_within_its_cadence_keeps_its_last_change(tmp_path: Path) -> None:
    """A skipped run retains nothing, so the cadence still counts from the real last change."""
    source = {**_replay_source(tmp_path, _html(_FIRST_LIST)), "check_frequency": "weekly"}
    fingerprint = _fingerprint(_html(_FIRST_LIST), source)
    ledger = RankingFreshnessLedger().observe(source, fingerprint, "2026-08-21")
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-08-24", ledger).collect([source])
    run = result.source_runs[0]
    assert run["status"] == "skipped"
    assert run["debug_metadata"]["safe_failure_status"] == "ranking_list_not_yet_refreshed"
    assert result.skipped_source_count == 1
    assert result.failed_source_count == 0
    assert result.source_mentions == []
    assert result.ranking_freshness is not None
    record = result.ranking_freshness.record_for(ranking_freshness_state_key(source))
    assert record is not None
    assert record.last_changed == "2026-08-21"
    assert editor_source_reason(run, "skipped") == "новый выпуск списка ещё не вышел"


def test_changed_fingerprint_passes() -> None:
    source = _source()
    ledger = RankingFreshnessLedger().observe(source, "abc", "2026-08-17")
    assert undated_freshness_failure(source, "def", ledger) == ("", "")


def test_a_dated_ranking_is_never_judged_on_movement() -> None:
    source = _source()
    source.pop("ranking_reporting_period_undated")
    ledger = RankingFreshnessLedger().observe(source, "abc", "2026-08-17")
    assert undated_freshness_failure(source, "abc", ledger) == ("", "")


# --- Ledger ----------------------------------------------------------------


def test_ledger_keeps_first_seen_and_moves_last_changed() -> None:
    source = _source()
    ledger = RankingFreshnessLedger().observe(source, "abc", "2026-08-17").observe(source, "def", "2026-08-24")
    record = ledger.record_for(ranking_freshness_state_key(source))
    assert record is not None
    assert record.first_seen == "2026-08-17"
    assert record.last_changed == "2026-08-24"
    assert record.fingerprint == "def"
    assert len(ledger.records) == 1


def test_ledger_round_trips_through_state_deterministically(tmp_path: Path) -> None:
    other = _source(name="Second undated ranking", url="https://example.test/otra/")
    ledger = RankingFreshnessLedger().observe(other, "zzz", "2026-08-10").observe(_source(), "abc", "2026-08-17")
    write_ranking_freshness(tmp_path, ledger)
    reloaded = load_ranking_freshness(tmp_path)
    assert reloaded == ledger
    records = read_state(tmp_path, "ranking_freshness")
    assert [record["state_key"] for record in records] == sorted(record["state_key"] for record in records)


def test_no_state_directory_yields_no_ledger() -> None:
    assert load_ranking_freshness(None) is None


def test_ledger_ignores_malformed_retained_records() -> None:
    ledger = RankingFreshnessLedger.from_records([{"state_key": "", "fingerprint": "abc"}, "not-a-record", 7])
    assert ledger.records == ()


# --- Collection ------------------------------------------------------------


def test_collector_error_without_fingerprint_preserves_original_error(tmp_path: Path, monkeypatch: Any) -> None:
    source = _source(extraction_method="synthetic-live")

    def fail_without_fingerprint(*_args: Any, **_kwargs: Any) -> Any:
        raise CollectorRunError("original collector failure")

    monkeypatch.setattr("autoscout.source_collection.source_candidates", fail_without_fingerprint)
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-08-24").collect([source])

    run = result.source_runs[0]
    assert run["reason"] == "original collector failure"
    assert "safe_failure_status" not in run["debug_metadata"]


_EXTRACTED_MENTION: dict[str, Any] = {
    "title": "HISTORIA ARGENTINA",
    "primary_authors": ["AUTORA UNA"],
    "isbn": "9780306406157",
    "current_rank": "1",
    "source_week": "2026-08-24",
    "source_url": URL,
    "evidence_url": URL,
    "signal_type": "bestseller/ranking",
    "signal_strength": "top ranking",
    "candidate_category": "adult nonfiction",
    "evidence_note": "Bounded ranking row on the captured source page.",
    "english_title": "Argentine History",
    "search_translation": "История Аргентины",
    "author_transliteration_note": "Автора Уна",
}


def _write_artifact(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _capture_payload(html: str) -> dict[str, Any]:
    """One Source Capture payload in the production shape, for every replay here.

    The source identity fields are what binds a capture to its configured
    source, so they belong in every capture rather than only in the one test
    that reads them.
    """
    return {
        "source_page": URL,
        "source_name": SOURCE_NAME,
        "source_url": URL,
        "resolved_url": URL,
        "reference_date": "2026-08-24",
        "html": html,
    }


def _replay_source_from(
    page_path: Path,
    extraction_path: Path,
    extraction_payload: dict[str, Any],
    **overrides: Any,
) -> dict[str, Any]:
    """Configure a ``codex-extraction-replay`` source over one capture pair."""
    _write_artifact(extraction_path, extraction_payload)
    return _source(
        source_page_fixture_file=str(page_path),
        extraction_response_file=str(extraction_path),
        extraction_method="codex-extraction-replay",
        **overrides,
    )


def _replay_source(tmp_path: Path, html: str, **overrides: Any) -> dict[str, Any]:
    """A replay whose extraction returns the one bounded row the page ranks first."""
    page_path = _write_artifact(tmp_path / f"page-{abs(hash(html)) % 10_000}.json", _capture_payload(html))
    return _replay_source_from(
        page_path,
        tmp_path / "extraction.json",
        {"source_page": URL, "mentions": [_EXTRACTED_MENTION]},
        **overrides,
    )


def _authoritative_empty_replay_source(tmp_path: Path, html: str, **overrides: Any) -> dict[str, Any]:
    """Build the production shape of an extraction that returned no entries.

    The artifact carries an empty ``entries`` list together with the operation
    identity and the capture digest that bind it to its Source Capture, which is
    what ``authoritative_empty_codex_artifact`` accepts as a real empty result
    rather than an error.  The collector therefore returns instead of raising,
    and the run reaches the Source Run Status decision.
    """
    page_path = _write_artifact(tmp_path / "empty-page.json", _capture_payload(html))
    return _replay_source_from(
        page_path,
        tmp_path / "empty-extraction.json",
        {
            "source_page": URL,
            "source_name": SOURCE_NAME,
            "source_url": URL,
            "operation_id": "op-issue-716",
            "capture_sha256": hashlib.sha256(page_path.read_bytes()).hexdigest(),
            "entries": [],
        },
        **overrides,
    )


def test_collection_fails_an_undated_source_closed_without_state(tmp_path: Path) -> None:
    source = _replay_source(tmp_path, _html(_FIRST_LIST))
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-08-24", None).collect([source])
    run = result.source_runs[0]
    assert run["status"] == "failed"
    assert "requires retained freshness state" in run["reason"]
    assert result.source_mentions == []


def test_collection_retains_the_first_observed_fingerprint(tmp_path: Path) -> None:
    source = _replay_source(tmp_path, _html(_FIRST_LIST))
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-08-24", RankingFreshnessLedger()).collect(
        [source]
    )
    assert result.source_runs[0]["status"] == "collected"
    assert result.ranking_freshness is not None
    record = result.ranking_freshness.record_for(ranking_freshness_state_key(source))
    assert record is not None
    assert record.fingerprint == _fingerprint(_html(_FIRST_LIST), source)


def test_collection_fails_closed_on_a_frozen_list(tmp_path: Path) -> None:
    source = _replay_source(tmp_path, _html(_FIRST_LIST))
    ledger = RankingFreshnessLedger().observe(source, _fingerprint(_html(_FIRST_LIST), source), "2026-08-17")
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-08-24", ledger).collect([source])
    run = result.source_runs[0]
    assert run["status"] == "failed"
    assert run["reason"] == "undated ranking list is unchanged since 2026-08-17 and cannot be reported as current"
    assert run["debug_metadata"]["safe_failure_status"] == "ranking_list_frozen"
    assert result.source_mentions == []


def test_collection_accepts_a_moved_list(tmp_path: Path) -> None:
    ledger = RankingFreshnessLedger().observe(_source(), "an-older-fingerprint", "2026-08-17")
    source = _replay_source(tmp_path, _html(_CHANGED_LIST))
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-08-24", ledger).collect([source])
    assert result.source_runs[0]["status"] == "collected"
    assert result.source_mentions
    assert result.ranking_freshness is not None
    record = result.ranking_freshness.record_for(ranking_freshness_state_key(source))
    assert record is not None
    assert record.fingerprint == _fingerprint(_html(_CHANGED_LIST), source)
    assert record.first_seen == "2026-08-17"
    assert record.last_changed == "2026-08-24"


def test_a_frozen_list_reports_frozen_even_when_its_mentions_are_dropped(tmp_path: Path) -> None:
    """A drop reason must not mask a frozen list.

    The retained extraction response claims rank 1 for a row the page now
    ranks second, so every mention is dropped.  The published reason must still
    be the frozen verdict, because that is what stops the run being reported.
    """
    source = _replay_source(tmp_path, _html(_MOVED_LIST))
    ledger = RankingFreshnessLedger().observe(source, _fingerprint(_html(_MOVED_LIST), source), "2026-08-17")
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-08-24", ledger).collect([source])
    run = result.source_runs[0]
    assert run["status"] == "failed"
    assert run["reason"] == "undated ranking list is unchanged since 2026-08-17 and cannot be reported as current"
    assert run["debug_metadata"]["safe_failure_status"] == "ranking_list_frozen"
    assert result.source_mentions == []


# --- Retention follows acceptance (issue #716) ------------------------------


def test_a_failed_empty_extraction_retains_no_fingerprint(tmp_path: Path) -> None:
    source = _authoritative_empty_replay_source(tmp_path, _html(_FIRST_LIST))
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-08-24", RankingFreshnessLedger()).collect(
        [source]
    )

    run = result.source_runs[0]
    assert run["status"] == "failed"
    assert "returned no entries" in run["reason"]
    assert result.ranking_freshness is not None
    assert result.ranking_freshness.record_for(ranking_freshness_state_key(source)) is None


def test_a_source_that_failed_recovers_on_the_next_run_against_the_same_page(tmp_path: Path) -> None:
    """The failure costs one run, not an open-ended outage.

    Week one returns an authoritative-empty artifact and fails.  Week two
    supplies a working extraction against the very same unchanged page, which
    before issue #716 was refused as frozen for a reason it could never clear.
    """
    week_one_dir = tmp_path / "week-one"
    week_one_dir.mkdir()
    failing = _authoritative_empty_replay_source(week_one_dir, _html(_FIRST_LIST))
    first = SourceCollection(tmp_path / "config.json", week_one_dir, "2026-08-24", RankingFreshnessLedger()).collect(
        [failing]
    )
    assert first.source_runs[0]["status"] == "failed"

    week_two_dir = tmp_path / "week-two"
    week_two_dir.mkdir()
    working = _replay_source(week_two_dir, _html(_FIRST_LIST))
    second = SourceCollection(tmp_path / "config.json", week_two_dir, "2026-08-31", first.ranking_freshness).collect(
        [working]
    )

    run = second.source_runs[0]
    assert run["status"] == "collected"
    assert second.source_mentions
    assert second.ranking_freshness is not None
    record = second.ranking_freshness.record_for(ranking_freshness_state_key(working))
    assert record is not None
    assert record.fingerprint == _fingerprint(_html(_FIRST_LIST), working)
    assert record.first_seen == "2026-08-31"


def test_a_failure_that_refuses_every_entry_retains_no_fingerprint(tmp_path: Path) -> None:
    """The rule is about acceptance, not about one failure mode.

    The retained extraction response claims rank 1 for a row this page ranks
    second, so every entry is refused on page shape.  That run collected
    nothing and may not claim the list was current.
    """
    source = _replay_source(tmp_path, _html(_MOVED_LIST))
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-08-24", RankingFreshnessLedger()).collect(
        [source]
    )

    run = result.source_runs[0]
    assert run["status"] == "failed"
    assert "unchanged since" not in run["reason"]
    assert result.source_mentions == []
    assert result.ranking_freshness is not None
    assert result.ranking_freshness.record_for(ranking_freshness_state_key(source)) is None


def test_default_onboarding_replay_runner_uses_isolated_state_for_undated_first_run(
    tmp_path: Path, monkeypatch: Any
) -> None:
    stage_root = tmp_path / "stage"
    config_path = stage_root / "configs" / "pilot.json"
    config_path.parent.mkdir(parents=True)
    source = _replay_source(tmp_path, _html(_FIRST_LIST))
    config_path.write_text(
        json.dumps({"run_date": "2026-08-24", "sources": [source]}, ensure_ascii=False),
        encoding="utf-8",
    )
    replay_output = stage_root / "tmp" / "issue-339-replay-validation"
    replay_state = stage_root / "tmp" / "issue-339-replay-validation-state"
    observed_calls: list[tuple[Path, Path, Path | None]] = []

    import autoscout.cli.weekly as cli

    real_run_mvp = cli.run_mvp

    def observed_run_mvp(observed_config: Path, observed_output: Path, *, state_dir: Path | None = None) -> None:
        observed_calls.append((observed_config, observed_output, state_dir))
        real_run_mvp(observed_config, observed_output, state_dir=state_dir)

    monkeypatch.setattr(cli, "run_mvp", observed_run_mvp)
    result = default_replay_runner(
        stage_root,
        {"config_paths": SimpleNamespace(pilot=config_path)},
        source,
    )

    assert result["status"] == "passed"
    assert observed_calls == [(config_path, replay_output, replay_state)]
    assert replay_output.is_relative_to(stage_root)
    assert replay_state.is_relative_to(stage_root)
    replay_records = read_state(replay_state, "ranking_freshness")
    assert len(replay_records) == 1
    assert replay_records[0]["fingerprint"] == _fingerprint(_html(_FIRST_LIST), source)
    assert replay_records[0]["state_key"] == ranking_freshness_state_key(source)
