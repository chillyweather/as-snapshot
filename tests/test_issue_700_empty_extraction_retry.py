"""Offline tests for issue #700: retry extraction when it returns nothing.

Replaying the preserved captures from run `4090c59a` showed extraction is
non-deterministic: Spiegel returned 0 entries in the run and 20 on replay
against byte-identical input, BookPage 0 then 11. The agent exits 0 either
way, so an empty result is indistinguishable downstream from a page that
genuinely published nothing.

The retry belongs at the extraction execution boundary. Production
collection runs `codex-extraction-replay`, whose collector reads the
already-written extraction JSON rather than invoking the agent, so a retry
placed in collection would re-read the same empty file twice.
"""

from __future__ import annotations

from typing import Any

from autoscout.codex_recovery import _should_reattempt
from autoscout.source_contribution import should_retry_empty_extraction


def _source(**overrides: Any) -> dict[str, Any]:
    source = {
        "name": "Spiegel Bestseller Sachbuch",
        "url": "https://example.test/list",
        "family": "bestseller/ranking",
    }
    source.update(overrides)
    return source


def _state(**overrides: Any) -> dict[str, Any]:
    state = {
        "valid": True,
        "eligible": True,
        "retryable": True,
        "timed_out": False,
        "empty_extraction": False,
        "empty_retry_used": False,
    }
    state.update(overrides)
    return state


# --- the predicate that scopes the cost ------------------------------------


def test_a_complete_list_source_that_extracted_nothing_is_retried() -> None:
    assert should_retry_empty_extraction(_source(), extracted_count=0) is True


def test_a_source_that_extracted_something_is_not_retried() -> None:
    assert should_retry_empty_extraction(_source(), extracted_count=20) is False


def test_a_source_that_may_stand_empty_is_not_retried() -> None:
    """An award page between cycles legitimately publishes nothing."""
    assert should_retry_empty_extraction(_source(may_report_no_entries=True), extracted_count=0) is False


def test_an_incremental_feed_is_not_retried() -> None:
    from autoscout.source_contribution import INCREMENTAL_FEED_FIELDS

    for field in INCREMENTAL_FEED_FIELDS:
        assert should_retry_empty_extraction(_source(**{field: "seen.json"}), extracted_count=0) is False, field


def test_the_retry_condition_matches_the_reason_it_prevents() -> None:
    """The retry fires exactly when the zero is attributed to extraction.

    Deliberate update for #700 part A: the single NO_ENTRY_EXTRACTED_REASON
    this pinned against no longer exists. The preserved invariant is that
    the retry fires on, and only on, the cases the seam attributes to
    extraction rather than to declarations, targets, or captures. A source
    with no URL fetched nothing, reproduces zero deterministically, and must
    neither be retried nor read as an extraction failure.

    A site-root URL stays retried and stays extraction-attributed: it may be
    a deliberate landing page (#679), so it only adds a note to the reason.
    Matched on the attributed prefix for that reason - the invariant is which
    condition the seam blames, not whether anything was appended to it."""
    from autoscout.source_contribution import AGENT_EMPTY_AFTER_RETRY_REASON
    from autoscout.source_contribution import AGENT_EMPTY_SINGLE_ATTEMPT_REASON
    from autoscout.source_contribution import zero_contribution_outcome

    attributed_prefixes = (
        AGENT_EMPTY_AFTER_RETRY_REASON.format(size="unknown size"),
        AGENT_EMPTY_SINGLE_ATTEMPT_REASON.format(size="unknown size"),
    )
    cases = (
        _source(),
        _source(may_report_no_entries=True),
        _source(seen_guids_file="seen.json"),
        {"name": "No URL"},
        _source(url="https://example.test/"),
    )
    for source in cases:
        status, reason = zero_contribution_outcome(
            source, extracted_count=0, valid_mention_count=0, admissible_mention_count=0
        )
        extraction_attributed = status == "failed" and reason.startswith(attributed_prefixes)
        assert should_retry_empty_extraction(source, extracted_count=0) is extraction_attributed, source


# --- the reattempt rule at the execution boundary --------------------------


def test_a_valid_but_empty_extraction_earns_one_more_attempt() -> None:
    """The historical rule only retried invalid attempts, so a valid artifact
    holding nothing was final. That is the bug."""
    assert _should_reattempt(_state(empty_extraction=True)) is True


def test_the_empty_retry_is_allowed_exactly_once() -> None:
    """A genuinely empty source costs one extra extraction, not a loop."""
    assert _should_reattempt(_state(empty_extraction=True, empty_retry_used=True)) is False


def test_a_valid_non_empty_extraction_is_never_reattempted() -> None:
    assert _should_reattempt(_state()) is False


def test_the_historical_failure_rule_is_unchanged() -> None:
    assert _should_reattempt(_state(valid=False)) is True
    assert _should_reattempt(_state(valid=False, retryable=False)) is False
    assert _should_reattempt(_state(valid=False, eligible=False)) is False
    assert _should_reattempt(_state(valid=False, timed_out=True)) is False


def test_an_empty_extraction_that_timed_out_is_not_reattempted() -> None:
    """A timeout already has its own handling and its own budget rules."""
    assert _should_reattempt(_state(empty_extraction=True, timed_out=True)) is False


# --- the retry must reach the persisted report -----------------------------


def _write_outcomes(tmp_path, states):
    from autoscout.codex_recovery import _write_extraction_outcomes

    _write_extraction_outcomes(tmp_path, states, [])
    import json

    from autoscout.codex_workflow import _extraction_outcomes

    return _extraction_outcomes(tmp_path), json


def _outcome_state(tmp_path, **overrides):
    artifact = tmp_path / "spiegel-codex-extraction.json"
    artifact.write_text('{"entries": []}', encoding="utf-8")
    state = {
        "item": {"source_name": "Spiegel Bestseller Sachbuch", "source_url": "https://example.test/l"},
        "expected": artifact.name,
        "artifact_path": artifact,
        "terminal": "validated",
        "submission_sequence": 1,
    }
    state.update(overrides)
    return state


def test_a_source_that_never_needed_a_retry_says_nothing_about_one(tmp_path) -> None:
    states = [_outcome_state(tmp_path)]

    outcomes, _ = _write_outcomes(tmp_path, states)
    record = outcomes["Spiegel Bestseller Sachbuch"]

    assert "empty_extraction_retry" not in record


# --- the guard must survive into the production worklist -------------------


def test_the_declarations_that_suppress_a_retry_travel_with_the_work_item() -> None:
    """Review finding 1.

    The retry guard reads the work item's source_metadata, but a work item
    only carries fields listed in SOURCE_ARTIFACT_METADATA_FIELDS. The three
    declarations that make an empty extraction legitimate were absent, so
    they serialized to {} and every legitimately empty source was retried in
    production. The suppression only ever worked in tests that injected the
    metadata by hand.
    """
    from autoscout.source_contribution import INCREMENTAL_FEED_FIELDS
    from autoscout.source_contribution import NO_ENTRIES_ALLOWED_FIELD
    from autoscout.source_metadata import portable_source_metadata

    source = {
        "name": "Award between cycles",
        NO_ENTRIES_ALLOWED_FIELD: True,
        "seen_guids_file": "seen.json",
    }
    metadata = portable_source_metadata(source)

    assert metadata.get(NO_ENTRIES_ALLOWED_FIELD) is True
    assert metadata.get("seen_guids_file") == "seen.json"
    for field in INCREMENTAL_FEED_FIELDS:
        assert field in __import__("autoscout.source_metadata", fromlist=["x"]).SOURCE_ARTIFACT_METADATA_FIELDS

    assert should_retry_empty_extraction(metadata, extracted_count=0) is False


# --- the report must state what actually happened --------------------------


def test_a_retry_refused_by_budget_is_not_reported_as_attempted(tmp_path) -> None:
    """Review finding 2.

    The allowance was spent when the state became eligible, before budget
    admission decided whether the retry could run at all. A source whose
    retry the budget refused still reported empty_extraction_retry: true,
    which would overstate recovery effort and hide budget pressure.
    """
    states = [_outcome_state(tmp_path, empty_retry_attempted=False, empty_retry_used=True)]

    outcomes, _ = _write_outcomes(tmp_path, states)
    record = outcomes["Spiegel Bestseller Sachbuch"]

    assert "empty_extraction_retry" not in record
    assert record.get("empty_extraction_retry_refused") is True


def test_a_retry_that_failed_is_not_reported_as_recovered(tmp_path) -> None:
    """Review finding 3.

    Recovery was computed as `not empty_extraction`, but an invalid second
    attempt also clears that flag: a retry that returned COMMAND_FAILED read
    as a recovery. Recovery must mean a validated, explicitly non-empty
    result.
    """
    states = [
        _outcome_state(
            tmp_path,
            empty_retry_attempted=True,
            empty_retry_used=True,
            empty_retry_recovered=False,
            terminal="command_failed",
        )
    ]

    outcomes, _ = _write_outcomes(tmp_path, states)
    record = outcomes["Spiegel Bestseller Sachbuch"]

    assert record["empty_extraction_retry"] is True
    assert record["empty_extraction_recovered"] is False


def test_a_retry_that_produced_entries_is_reported_as_recovered(tmp_path) -> None:
    """Spiegel's real shape: empty, retried, then a validated list of 20."""
    states = [
        _outcome_state(
            tmp_path,
            empty_retry_attempted=True,
            empty_retry_used=True,
            empty_retry_recovered=True,
        )
    ]

    outcomes, _ = _write_outcomes(tmp_path, states)
    record = outcomes["Spiegel Bestseller Sachbuch"]

    assert record["empty_extraction_retry"] is True
    assert record["empty_extraction_recovered"] is True
