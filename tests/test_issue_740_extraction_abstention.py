"""Issue #740: extraction must not silently abstain from a list it has read.

The pinned model returned ``entries: []`` in 18 of 93 qualification runs,
including all three TodosTusLibros runs whose own tool output listed every
book.  The generic contract offered an empty exit ("Return an empty mentions
array if no eligible ... mentions are present"), and an empty artifact
validated as a healthy empty page.

These tests pin down the three changes: the contract asks for completeness
and a stated empty reason, an empty answer from a capture whose rows code can
count is refused and retried, and the extractor's stated reason reaches the
run record.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from autoscout import model_qualification as mq
from autoscout.codex_recovery import ExtractionAttempt
from autoscout.codex_recovery import ExtractionStatus
from autoscout.codex_recovery import ProductionCodexAdapter
from autoscout.codex_recovery import build_codex_extraction_prompt
from autoscout.extraction_completeness import capture_has_listing_rows
from autoscout.extraction_completeness import capture_listing_row_count
from autoscout.extraction_completeness import extractor_empty_reason
from autoscout.observed_extraction import validate_extraction_artifact
from autoscout.source_collectors.contract import EMPTY_REASON_RULE
from autoscout.source_collectors.contract import EXTRACTION_COMPLETENESS_RULE
from autoscout.source_collectors.contract import EXTRACTION_EMPTY_RESULT_RULE
from autoscout.source_collectors.contract import extraction_contract
from autoscout.source_contribution import zero_contribution_outcome

BOKHANDLER = "Bokhandlerforeningen Boklista - Generell litteratur voksne"
OLD_EMPTY_EXIT = "Return an empty mentions array if no eligible adult nonfiction book/work mentions are present."


@pytest.fixture(scope="module")
def sources() -> dict[str, dict[str, Any]]:
    # Boklista is retired since 2026-11-15; its extraction contract and frozen
    # benchmark case remain the recorded abstention case, so retired sources
    # are included.
    return mq.configured_sources(include_inactive=True)


@pytest.fixture(scope="module")
def boklista_case() -> mq.BenchmarkCase:
    return next(case for case in mq.load_benchmark() if case.source_name == BOKHANDLER)


def _envelope(item: dict[str, Any], entries: list[Any], **extra: Any) -> dict[str, Any]:
    return {
        "source_name": item["source_name"],
        "source_url": item["source_url"],
        "operation_id": item["operation_id"],
        "capture_sha256": item["capture_sha256"],
        "entries": entries,
        **extra,
    }


def test_no_configured_contract_offers_the_old_empty_exit(sources) -> None:
    for name, source in sources.items():
        contract = extraction_contract(source, capture_file="capture.json")
        rules = contract["rules"]
        assert OLD_EMPTY_EXIT not in rules, name
        assert EXTRACTION_COMPLETENESS_RULE in rules, name
        assert EXTRACTION_EMPTY_RESULT_RULE in rules, name
        assert EMPTY_REASON_RULE in rules, name
        assert "empty_reason" in contract["required_output_shape"], name
        # empty_reason is optional: it must never become a required field.
        assert "empty_reason" not in contract["required_top_level_fields"], name


def test_the_audible_contract_states_the_narrative_fallback(sources) -> None:
    # The qualification run's only stated empty_reason for Audible was "The
    # capture lacks explicit narrative classifications for the chart
    # entries": the chart shows no genre labels, and the rule never named the
    # fallback that every accepted reference entry uses.
    contract = extraction_contract(sources["Audible Bestselling Business & Careers Audiobooks"])
    rules = "\n".join(contract["rules"])

    assert 'otherwise use "other nonfiction"' in rules
    assert "never infer one from the title alone" in rules
    assert "other nonfiction" in contract["required_output_shape"]["mentions"][0]["narrative_classification"]


def test_the_inline_contract_has_no_envelope_field(sources) -> None:
    contract = extraction_contract(sources[BOKHANDLER])

    assert EXTRACTION_COMPLETENESS_RULE in contract["rules"]
    assert EMPTY_REASON_RULE not in contract["rules"]
    assert "empty_reason" not in contract["required_output_shape"]


def test_the_codex_prompt_ends_with_the_completeness_check(sources, boklista_case) -> None:
    item = mq.qualification_work_item(sources[BOKHANDLER], boklista_case.capture_file, boklista_case.capture_sha256)
    prompt = build_codex_extraction_prompt(item)

    assert "every row that meets the required fields must be in entries" in prompt
    assert "add a top-level empty_reason" in prompt
    assert OLD_EMPTY_EXIT not in prompt


def test_rows_are_counted_only_where_a_row_selector_is_declared(sources, boklista_case) -> None:
    capture = json.loads(boklista_case.capture_bytes().decode("utf-8"))
    source = sources[BOKHANDLER]
    item = mq.qualification_work_item(source, boklista_case.capture_file, boklista_case.capture_sha256)

    flat = capture_listing_row_count(source, capture)
    assert flat is not None and flat >= boklista_case.reference_count
    # A work item nests the declaration under source_metadata.
    assert capture_listing_row_count(item, capture) == flat
    assert capture_has_listing_rows(item, capture) is True
    assert capture_listing_row_count(source, {**capture, "html": ""}) == 0

    generic = sources["USA TODAY Bestselling Books Nonfiction"]
    assert capture_listing_row_count(generic, capture) is None
    assert capture_has_listing_rows(generic, capture) is False


CUSPIDE = "Cúspide 100 más vendidos"


@pytest.fixture(scope="module")
def cuspide_case() -> mq.BenchmarkCase:
    return next(case for case in mq.load_benchmark() if case.source_name == CUSPIDE)


def test_a_detail_category_ranking_counts_only_allowlisted_rows(sources, cuspide_case) -> None:
    # Title exclusions keep every row for a detail-category route, so the
    # count must come from the captured detail pages' allowlisted category.
    from autoscout.source_page_identity import ranking_rows

    capture = json.loads(cuspide_case.capture_bytes().decode("utf-8"))
    source = sources[CUSPIDE]
    count = capture_listing_row_count(source, capture)

    assert len(ranking_rows(capture["html"], source)) == 100
    assert count is not None and cuspide_case.reference_count <= count < 100
    # A row whose detail page was not captured cannot be shown eligible.
    assert capture_listing_row_count(source, {**capture, "detail_pages": []}) == 0


def test_an_all_excluded_detail_category_page_may_be_empty(tmp_path: Path, sources, cuspide_case) -> None:
    source = {**sources[CUSPIDE], "ranking_detail_category_allowlist": ["No Such Category"]}
    item = mq.qualification_work_item(source, cuspide_case.capture_file, cuspide_case.capture_sha256)
    capture_path = tmp_path / cuspide_case.capture_file
    capture_path.write_bytes(cuspide_case.capture_bytes())
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    artifact = tmp_path / "artifact.json"
    artifact.write_text(json.dumps(_envelope(item, [], empty_reason="No row is in an allowed category.")), "utf-8")

    assert capture_listing_row_count(item, capture) == 0
    assert capture_has_listing_rows(item, capture) is False
    observation = validate_extraction_artifact(artifact, item, capture_path)
    assert observation.diagnostic_class != "unexpected_empty_extraction"
    assert ProductionCodexAdapter(agent=object()).validate_extraction_artifact(artifact, item, capture_path) is True


def test_an_empty_artifact_from_a_countable_capture_is_refused(tmp_path: Path, sources, boklista_case) -> None:
    item = mq.qualification_work_item(sources[BOKHANDLER], boklista_case.capture_file, boklista_case.capture_sha256)
    capture_path = tmp_path / boklista_case.capture_file
    capture_path.write_bytes(boklista_case.capture_bytes())
    artifact = tmp_path / "artifact.json"
    artifact.write_text(json.dumps(_envelope(item, [], empty_reason="No rows qualify.")), encoding="utf-8")

    observation = validate_extraction_artifact(artifact, item, capture_path)
    assert observation.valid is False
    assert observation.diagnostic_class == "unexpected_empty_extraction"

    adapter = ProductionCodexAdapter(agent=object())
    assert adapter.validate_extraction_artifact(artifact, item, capture_path) is False


def test_an_empty_artifact_from_an_uncountable_capture_still_validates(tmp_path: Path, sources) -> None:
    case = next(case for case in mq.load_benchmark() if case.source_name == "USA TODAY Bestselling Books Nonfiction")
    item = mq.qualification_work_item(sources[case.source_name], case.capture_file, case.capture_sha256)
    capture_path = tmp_path / case.capture_file
    capture_path.write_bytes(case.capture_bytes())
    artifact = tmp_path / "artifact.json"
    artifact.write_text(json.dumps(_envelope(item, [])), encoding="utf-8")

    # Unchanged behaviour: code cannot count this page's rows, so an empty
    # answer is still judged by the existing empty-extraction retry (#700).
    assert validate_extraction_artifact(artifact, item, capture_path).valid is True


def test_production_retries_an_abstention_and_keeps_the_recovered_list(tmp_path: Path, sources, boklista_case) -> None:
    item = mq.qualification_work_item(sources[BOKHANDLER], boklista_case.capture_file, boklista_case.capture_sha256)
    item["expected_extraction_file"] = "boklista-codex-extraction.json"
    (tmp_path / boklista_case.capture_file).write_bytes(boklista_case.capture_bytes())
    (tmp_path / "codex-extraction-worklist.json").write_text(json.dumps({"work_items": [item]}), encoding="utf-8")
    entries = boklista_case.reference()["entries"]
    calls: list[int] = []

    class AbstainOnceAgent:
        def extract_with_timeout(self, work_item, capture_path, artifact_path, *, timeout_seconds):
            del capture_path, timeout_seconds
            calls.append(1)
            payload = [] if len(calls) == 1 else entries
            artifact_path.write_text(
                json.dumps(
                    _envelope(work_item, payload, empty_reason="No rows qualify.")
                    if not payload
                    else _envelope(work_item, payload),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return ExtractionAttempt(ExtractionStatus.PRODUCED, True, 0)

    result = ProductionCodexAdapter(
        AbstainOnceAgent(),
        runtime_budget_seconds=1000,
        per_attempt_timeout_seconds=10,
        max_attempts=2,
        max_workers=1,
        sleeper=lambda seconds: None,
        retry_headroom_seconds=0,
    ).fulfill(tmp_path)

    assert len(calls) == 2
    assert result["ready_items"] == 1
    published = json.loads((tmp_path / "boklista-codex-extraction.json").read_text(encoding="utf-8"))
    assert len(published["entries"]) == len(entries)


def test_the_extractor_reason_is_bounded_and_single_line() -> None:
    assert extractor_empty_reason({"entries": [], "empty_reason": "  No\nnonfiction   rows. "}) == "No nonfiction rows."
    assert len(extractor_empty_reason({"empty_reason": "x" * 1000})) == 300
    assert extractor_empty_reason({"empty_reason": 5}) == ""
    assert extractor_empty_reason([]) == ""


def test_the_run_record_quotes_the_extractor_reason() -> None:
    source = {"name": "Source", "url": "https://example.test/list"}
    status, reason = zero_contribution_outcome(
        source,
        extracted_count=0,
        valid_mention_count=0,
        admissible_mention_count=0,
        debug_metadata={
            "capture_html_bytes": 1000,
            "empty_extraction_retry": True,
            "extractor_empty_reason": "The page lists only fiction.",
        },
    )

    assert status == "failed"
    assert reason.endswith('; the extractor said: "The page lists only fiction."')
    assert "no entries on both attempts" in reason


def test_benchmark_captures_are_unchanged(boklista_case) -> None:
    # The count above is taken from the frozen benchmark capture; its digest
    # is what the qualification baseline was measured on.
    assert hashlib.sha256(boklista_case.capture_bytes()).hexdigest() == boklista_case.capture_sha256


def test_collection_quotes_the_empty_reason_of_an_accepted_empty_artifact(tmp_path: Path) -> None:
    from autoscout.source_collection import SourceCollection

    url = "https://example.test/nonfiction-list/"
    name = "Synthetic generic list"
    page = {
        "collection_mode": "codex-capture",
        "source_url": url,
        "source_name": name,
        "source_page": url,
        "resolved_url": url,
        "reference_date": "2026-09-27",
        "html": "<html><body><h1>Nonfiction</h1><p>Nothing listed this week.</p></body></html>",
    }
    page_path = tmp_path / "page.json"
    page_path.write_text(json.dumps(page), encoding="utf-8")
    extraction_path = tmp_path / "extraction.json"
    extraction_path.write_text(
        json.dumps(
            {
                "source_name": name,
                "source_url": url,
                "operation_id": "op-issue-740",
                "capture_sha256": hashlib.sha256(page_path.read_bytes()).hexdigest(),
                "entries": [],
                "empty_reason": "The page lists no books this week.",
            }
        ),
        encoding="utf-8",
    )
    source = {
        "name": name,
        "url": url,
        "family": "media/curated list",
        "language": "en",
        "extraction_method": "codex-extraction-replay",
        "source_page_fixture_file": str(page_path),
        "extraction_response_file": str(extraction_path),
    }

    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-09-27", None).collect([source])

    run = result.source_runs[0]
    assert run["status"] == "failed"
    assert run["reason"].endswith('; the extractor said: "The page lists no books this week."')
