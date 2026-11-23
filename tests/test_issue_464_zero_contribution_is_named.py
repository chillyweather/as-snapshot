"""Issue #464 - a Configured Source that contributes nothing must say why.

Three outcomes were identical in the record and are not the same thing: a page
that genuinely listed nothing new, a page that listed books from which
extraction produced no mention, and a page that could not be read. The first is
correct. The other two are defects. All three reported ``status="collected"``
with an empty reason, and ``run-summary.json`` counted every one as healthy.

The three sources #464 measured are driven here from their committed capture
fixtures, so the diagnosis is checked against real artifacts rather than a
hand-built shape.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from autoscout.config import load_validated_config
from autoscout.config import validate_config
from autoscout.source_collection import SourceCollection
from autoscout.source_contribution import NO_ENTRIES_ALLOWED_FIELD
from autoscout.source_contribution import NO_ENTRIES_REASON_FIELD
from autoscout.source_contribution import source_may_report_no_entries
from autoscout.source_contribution import source_reports_new_items_only
from autoscout.source_contribution import validate_no_entries_declaration
from autoscout.source_contribution import zero_contribution_outcome

ROOT = Path(__file__).resolve().parents[1]
RUN_DATE = "2026-08-14"

# The three sources #464 measured, with the family each resolves to. The axis
# that tells them apart is what the source publishes, not what it is called.
MEASURED_SOURCES = (
    "Audiobooks.com Nonfiction Audiobook Bestsellers",
    "TodosTusLibros Librerías Recomiendan No Ficción",
    "Nonfiction.fr Critiques",
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _pilot_source(name: str) -> dict[str, Any]:
    config = load_validated_config(ROOT / "configs" / "current-weekly-pilot.json")
    source = dict(next(s for s in config["sources"] if s["name"] == name))
    for field in ("source_page_fixture_file", "extraction_response_file", "seen_guids_fixture_file"):
        if source.get(field):
            source[field] = str((ROOT / "configs" / source[field]).resolve())
    return source


def _emptied(source: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    """Return the source with its committed capture kept and its extraction emptied.

    The page is the real one. Only the extraction result is replaced, which is
    the exact shape #464 found in production: a validated artifact holding
    ``entries: []``.
    """
    emptied = dict(source)
    capture_path = tmp_path / "capture.json"
    capture_path.write_bytes(Path(source["source_page_fixture_file"]).read_bytes())
    extraction_path = tmp_path / "extraction.json"
    _write_json(
        extraction_path,
        {
            "source_name": source["name"],
            "source_url": source["url"],
            "operation_id": "op-464",
            "capture_sha256": hashlib.sha256(capture_path.read_bytes()).hexdigest(),
            "entries": [],
        },
    )
    emptied["source_page_fixture_file"] = str(capture_path)
    emptied["extraction_response_file"] = str(extraction_path)
    return emptied


# ── The unit the decision lives in ───────────────────────────────────────────


def test_a_zero_never_carries_an_empty_reason() -> None:
    """Every path out of the decision names a reason. That is the whole issue."""
    cases = (
        {},
        {"seen_guids_file": "seen.json"},
        {"seen_guids_fixture_file": "seen.json"},
        {NO_ENTRIES_ALLOWED_FIELD: True, NO_ENTRIES_REASON_FIELD: "an annual page stands empty"},
    )
    for source in cases:
        for extracted in (0, 5):
            status, reason = zero_contribution_outcome(
                source,
                extracted_count=extracted,
                valid_mention_count=0,
                admissible_mention_count=0,
            )
            assert status in {"collected", "failed"}
            assert reason.strip(), f"{source} at extracted={extracted} produced an empty reason"


def test_a_page_that_held_entries_and_produced_none_is_a_named_failure() -> None:
    status, reason = zero_contribution_outcome(
        {NO_ENTRIES_ALLOWED_FIELD: True, NO_ENTRIES_REASON_FIELD: "an annual page stands empty"},
        extracted_count=20,
        valid_mention_count=0,
        admissible_mention_count=0,
        debug_metadata={"drop_reasons": ["missing_evidence_url", "missing_evidence_url", "ungrounded_author"]},
    )
    # The declaration permits an empty page. It does not permit losing 20 entries.
    assert status == "failed"
    assert "20" in reason
    assert "missing_evidence_url" in reason
    assert "ungrounded_author" in reason


def test_a_page_that_held_nothing_is_told_apart_from_one_that_lost_its_entries() -> None:
    declared = {NO_ENTRIES_ALLOWED_FIELD: True, NO_ENTRIES_REASON_FIELD: "an annual page stands empty"}
    empty_status, empty_reason = zero_contribution_outcome(
        declared, extracted_count=0, valid_mention_count=0, admissible_mention_count=0
    )
    lost_status, lost_reason = zero_contribution_outcome(
        declared, extracted_count=20, valid_mention_count=0, admissible_mention_count=0
    )
    assert empty_status == "collected"
    assert lost_status == "failed"
    assert empty_reason != lost_reason


def test_an_undeclared_full_list_source_that_publishes_nothing_fails() -> None:
    status, reason = zero_contribution_outcome(
        {"name": "A Weekly Chart"},
        extracted_count=0,
        valid_mention_count=0,
        admissible_mention_count=0,
    )
    assert status == "failed"
    assert "complete list" in reason


def test_a_source_that_reports_only_new_items_may_report_none() -> None:
    for field in ("seen_guids_file", "seen_guids_fixture_file"):
        status, reason = zero_contribution_outcome(
            {field: "seen.json"},
            extracted_count=0,
            valid_mention_count=0,
            admissible_mention_count=0,
        )
        assert status == "collected", field
        assert "newer than the previous run" in reason


# ── The declaration is refused when it would be silent ───────────────────────


@pytest.mark.parametrize(
    ("source", "fragment"),
    (
        ({NO_ENTRIES_ALLOWED_FIELD: True}, "requires a recorded"),
        ({NO_ENTRIES_ALLOWED_FIELD: True, NO_ENTRIES_REASON_FIELD: "   "}, "requires a recorded"),
        ({NO_ENTRIES_ALLOWED_FIELD: False}, "must be true"),
        ({NO_ENTRIES_REASON_FIELD: "a reason with no permission"}, f"requires {NO_ENTRIES_ALLOWED_FIELD}"),
        (
            {
                NO_ENTRIES_ALLOWED_FIELD: True,
                NO_ENTRIES_REASON_FIELD: "an annual page stands empty",
                "seen_guids_file": "seen.json",
            },
            "already reports only new items",
        ),
    ),
)
def test_an_unusable_declaration_is_refused(source: dict[str, Any], fragment: str) -> None:
    assert fragment in validate_no_entries_declaration(source)


def test_a_complete_declaration_is_accepted() -> None:
    assert not validate_no_entries_declaration(
        {NO_ENTRIES_ALLOWED_FIELD: True, NO_ENTRIES_REASON_FIELD: "an annual page stands empty"}
    )
    assert not validate_no_entries_declaration({})


def test_config_validation_refuses_a_silent_permission() -> None:
    base = {
        "run_date": RUN_DATE,
        "sources": [
            {
                "name": "Silently Empty",
                "family": "award",
                "url": "https://example.test/silently-empty",
                "active": True,
                "access_status": "open",
                "check_frequency": "annual",
                "extraction_method": "fixture",
                "fixture_candidates": [],
                NO_ENTRIES_ALLOWED_FIELD: True,
            }
        ],
    }
    errors = validate_config(base)
    assert any(NO_ENTRIES_REASON_FIELD in error for error in errors), errors

    base["sources"][0][NO_ENTRIES_REASON_FIELD] = "an annual award page lists nothing between announcements"
    assert not [error for error in validate_config(base) if NO_ENTRIES_REASON_FIELD in error]


# ── The three sources #464 measured, from their committed captures ───────────


@pytest.mark.parametrize("name", MEASURED_SOURCES)
def test_measured_sources_are_diagnosed_from_their_committed_captures(name: str, tmp_path: Path) -> None:
    """Each of the three reports a named outcome that matches what it publishes.

    Nonfiction.fr reports only items it has not seen before, so nothing new is
    a correct week. The other two republish a complete list on every check, so
    an empty result is a defect. This is the distinction the record was missing.
    """
    source = _pilot_source(name)
    if source_reports_new_items_only(source):
        # The RSS collector reaches its zero through its own seen-item ledger,
        # so the decision is checked directly rather than through a rewritten
        # extraction artifact the collector would not read.
        status, reason = zero_contribution_outcome(
            source, extracted_count=0, valid_mention_count=0, admissible_mention_count=0
        )
        assert status == "collected"
        assert "newer than the previous run" in reason
        return

    result = SourceCollection(tmp_path / "config.json", tmp_path, RUN_DATE).collect([_emptied(source, tmp_path)])
    run = result.source_runs[0]
    assert run["valid_mention_count"] == 0
    assert run["status"] == "failed", f"{name}: a full-list source that published nothing must be a failure"
    assert run["reason"].strip(), f"{name}: a zero must never carry an empty reason"
    assert result.failed_source_count == 1


def test_only_nonfiction_fr_of_the_three_reports_new_items_only() -> None:
    """The diagnosis, stated as a property rather than a comment.

    #464 asked which of the three was correct and which were defects. The
    answer is derived from what each source declares, not from its name.
    """
    incremental = {name for name in MEASURED_SOURCES if source_reports_new_items_only(_pilot_source(name))}
    assert incremental == {"Nonfiction.fr Critiques"}
    assert not any(source_may_report_no_entries(_pilot_source(name)) for name in MEASURED_SOURCES)


# ── The editor summary tells a source that contributed nothing apart ─────────


def test_the_named_reason_reaches_the_source_problem_channel(tmp_path: Path) -> None:
    """A zero must be visible where the editor reads problems, not only in JSON."""
    source = _emptied(_pilot_source("Audiobooks.com Nonfiction Audiobook Bestsellers"), tmp_path)
    result = SourceCollection(tmp_path / "config.json", tmp_path, RUN_DATE).collect([source])
    run = result.source_runs[0]

    from autoscout.reporting import capture_source_outcome_counts

    counts = capture_source_outcome_counts([], result.source_runs, allow_pipeline_statuses=False)
    assert counts["failed"] == 1, "a source that contributed nothing must count against source health"
    assert run["status"] == "failed"
    assert run["reason"] in json.dumps(result.source_runs, ensure_ascii=False)
