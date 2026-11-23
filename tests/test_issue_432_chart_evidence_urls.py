"""Offline regression coverage for Issues #432 and #438 chart evidence URLs."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from autoscout.config import load_validated_config
from autoscout.source_collection import SourceCollection
from autoscout.source_collectors import CollectorRunError
from autoscout.source_collectors import agent_extraction_mentions_from_response
from autoscout.source_collectors import extraction_contract_violation
from autoscout.source_collectors import normalize_chart_evidence_url
from autoscout.source_collectors import normalized_agent_mention

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "configs/current-weekly-pilot.json"
RUN_DIR = Path(__file__).resolve().parent / "fixtures" / "issue-432-chart-baseline"
RUN_DATE = "2026-08-13"

CHART_ARTIFACTS = (
    (
        "Audible Bestselling Science & Engineering Audiobooks",
        "codex-capture-audible-bestselling-science---engineering-audiobooks-88b04707.json",
        "audible-bestselling-science---engineering-audiobooks-88b04707-codex-extraction.json",
    ),
    (
        "Audible Bestselling Business & Careers Audiobooks",
        "codex-capture-audible-bestselling-business---careers-audiobooks-81782741.json",
        "audible-bestselling-business---careers-audiobooks-81782741-codex-extraction.json",
    ),
    (
        "Audible Bestselling Computers & Technology Audiobooks",
        "codex-capture-audible-bestselling-computers---technology-audiobooks-b6b4652c.json",
        "audible-bestselling-computers---technology-audiobooks-b6b4652c-codex-extraction.json",
    ),
    (
        "Amazon Charts Most Read Nonfiction",
        "codex-capture-amazon-charts-most-read-nonfiction-bc5f0a19.json",
        "amazon-charts-most-read-nonfiction-bc5f0a19-codex-extraction.json",
    ),
    (
        "Amazon.de Charts Meistgelesene Sachbücher",
        "codex-capture-amazon-de-charts-meistgelesene-sachbücher-40c7327c.json",
        "amazon-de-charts-meistgelesene-sachbücher-40c7327c-codex-extraction.json",
    ),
)

CURRENT_RUN_SUCCESS_CHART_ARTIFACTS = tuple(
    artifact for artifact in CHART_ARTIFACTS if artifact[0] != "Audible Bestselling Business & Careers Audiobooks"
)


def _source(name: str, *, extraction_path: Path | None = None) -> dict[str, Any]:
    config = load_validated_config(CONFIG_PATH)
    source = next(source for source in config["sources"] if source["name"] == name)
    artifact = next(artifact for artifact in CHART_ARTIFACTS if artifact[0] == name)
    return {
        **source,
        "source_page_fixture_file": str(RUN_DIR / artifact[1]),
        "extraction_response_file": str(extraction_path or RUN_DIR / artifact[2]),
    }


def _collect(source: dict[str, Any]):
    return SourceCollection(CONFIG_PATH, ROOT, RUN_DATE).collect([source])


def _current_run_regression_source(name: str) -> dict[str, Any]:
    return _source(name)


def _entries(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "mentions" in data:
        return data["mentions"]
    return data["entries"]


def test_amazon_chart_normalization_resolves_before_stripping_tracking() -> None:
    source = _source("Amazon Charts Most Read Nonfiction")
    expected = "https://www.amazon.com/dp/1668067242"

    for evidence_url in (
        "/dp/1668067242/ref=chrt_bk_rd_nf_1_ci_lp",
        "https://www.amazon.com/dp/1668067242/ref=chrt_bk_rd_nf_1_ci_lp",
    ):
        assert normalize_chart_evidence_url(evidence_url, source) == expected


def test_amazon_chart_collection_stores_relative_and_absolute_urls_identically(tmp_path: Path) -> None:
    name = "Amazon Charts Most Read Nonfiction"
    expected = "https://www.amazon.com/dp/1668067242"
    relative_result = _collect(_source(name))
    assert relative_result.source_runs[0]["status"] == "collected"
    assert relative_result.source_mentions[0]["evidence_url"] == expected

    extraction = json.loads(
        (RUN_DIR / "amazon-charts-most-read-nonfiction-bc5f0a19-codex-extraction.json").read_text(encoding="utf-8")
    )
    extraction["entries"][0]["evidence_url"] = expected + "/ref=chrt_bk_rd_nf_1_ci_lp"
    extraction_path = tmp_path / "amazon-absolute-evidence.json"
    extraction_path.write_text(json.dumps(extraction), encoding="utf-8")

    absolute_result = _collect(_source(name, extraction_path=extraction_path))
    assert absolute_result.source_runs[0]["status"] == "collected"
    assert absolute_result.source_mentions[0]["evidence_url"] == expected


@pytest.mark.parametrize("name, _page_file, _extraction_file", CURRENT_RUN_SUCCESS_CHART_ARTIFACTS)
def test_current_run_chart_artifacts_collect_every_raw_entry(name: str, _page_file: str, _extraction_file: str) -> None:
    source = _current_run_regression_source(name)
    expected_entries = _entries(Path(source["extraction_response_file"]))
    expected_ranks = [int(entry["current_rank"]) for entry in expected_entries]
    result = _collect(source)

    assert result.source_runs[0]["status"] == "collected"
    assert result.source_runs[0]["raw_mention_count"] == len(expected_entries)
    assert result.source_runs[0]["valid_mention_count"] == len(expected_entries)
    assert len(result.source_mentions) == len(expected_entries)
    assert [int(mention["current_rank"]) for mention in result.source_mentions] == expected_ranks

    source = _source(name)
    if source.get("amazon_charts_weekly") is True:
        for mention in result.source_mentions:
            parsed = urlparse(mention["evidence_url"])
            assert parsed.hostname == urlparse(source["url"]).hostname
            assert parsed.path.casefold().startswith("/dp/")
            assert len(parsed.path.rsplit("/", 1)[-1]) == 10
            assert not parsed.query
            assert not parsed.fragment
            assert "/ref" not in mention["evidence_url"]
    else:
        for mention in result.source_mentions:
            parsed = urlparse(mention["evidence_url"])
            assert parsed.hostname == urlparse(source["url"]).hostname
            assert parsed.path.startswith("/pd/")
            assert parsed.scheme in {"http", "https"}


def test_business_chart_grounding_failure_remains_out_of_scope(tmp_path: Path) -> None:
    extraction = json.loads(
        (RUN_DIR / "audible-bestselling-business---careers-audiobooks-81782741-codex-extraction.json").read_text(
            encoding="utf-8"
        )
    )
    # Five ungrounded rows of 20 (75% kept) still fail the chart; one would
    # now be dropped and the rest kept (Issue #839, #751 decision C).
    for entry in extraction["entries"][:5]:
        entry["title"] = "Deliberately Ungrounded Test Title"
        entry["primary_authors"] = ["Deliberately Ungrounded Test Author"]
    extraction_path = tmp_path / "business-ungrounded-extraction.json"
    extraction_path.write_text(json.dumps(extraction), encoding="utf-8")

    result = _collect(_source("Audible Bestselling Business & Careers Audiobooks", extraction_path=extraction_path))

    assert result.source_runs[0]["status"] == "failed"
    assert result.source_runs[0]["valid_mention_count"] == len(extraction["entries"]) - 5
    assert "not grounded in one entry" in result.source_runs[0]["reason"]


def test_amazon_foreign_host_and_missing_asin_stay_rejected() -> None:
    name = "Amazon Charts Most Read Nonfiction"
    source = _source(name)
    page = json.loads(Path(source["source_page_fixture_file"]).read_text(encoding="utf-8"))
    extraction_path = RUN_DIR / "amazon-charts-most-read-nonfiction-bc5f0a19-codex-extraction.json"
    entry = _entries(extraction_path)[0]

    for evidence_url in (
        "https://foreign.example/dp/1668067242/ref=tracking",
        "https://www.amazon.com/dp/not-an-asin/ref=tracking",
    ):
        invalid = {**entry, "evidence_url": evidence_url}
        normalized = normalized_agent_mention(invalid, source, RUN_DATE)
        reason = extraction_contract_violation(invalid, normalized, source, page)
        assert "direct Amazon /dp/ product URL" in reason


def test_wrong_rank_set_remains_a_rank_failure() -> None:
    name = "Amazon Charts Most Read Nonfiction"
    source = _source(name)
    page = json.loads(Path(source["source_page_fixture_file"]).read_text(encoding="utf-8"))
    extraction = json.loads(
        (RUN_DIR / "amazon-charts-most-read-nonfiction-bc5f0a19-codex-extraction.json").read_text(encoding="utf-8")
    )
    extraction["entries"][0]["current_rank"] = 2
    page["html"] = re.sub(
        r'(<div id="rank1".*?<div class="kc-rank-card-rank">\s*)1(\s*</div>)',
        r"\g<1>2\g<2>",
        page["html"],
        count=1,
        flags=re.DOTALL,
    ).replace('id="rank1"', 'id="rank2"', 1)

    with pytest.raises(CollectorRunError, match="expected exact ranks 1 through 20") as exc_info:
        agent_extraction_mentions_from_response(page, extraction, source, RUN_DATE)

    assert exc_info.value.debug_metadata["safe_failure_status"] == "page_shape"


def test_all_audible_contract_rejections_report_rule_and_count(tmp_path: Path) -> None:
    name = "Audible Bestselling Science & Engineering Audiobooks"
    extraction_path = tmp_path / "invalid-evidence.json"
    extraction = json.loads(
        (RUN_DIR / "audible-bestselling-science---engineering-audiobooks-88b04707-codex-extraction.json").read_text(
            encoding="utf-8"
        )
    )
    for entry in extraction["entries"]:
        entry["evidence_url"] = "https://www.audible.com/not-a-product"
    extraction_path.write_text(json.dumps(extraction), encoding="utf-8")

    result = _collect(_source(name, extraction_path=extraction_path))

    assert result.source_runs[0]["status"] == "failed"
    reason = result.source_runs[0]["reason"]
    assert "Audible explicit chart evidence URL is not a direct Audible /pd/ product URL" in reason
    assert "20" in reason
    assert "exact ranks 1 through 20" not in reason


def test_all_amazon_contract_rejections_report_rule_and_count(tmp_path: Path) -> None:
    name = "Amazon Charts Most Read Nonfiction"
    extraction_path = tmp_path / "invalid-evidence.json"
    extraction = json.loads(
        (RUN_DIR / "amazon-charts-most-read-nonfiction-bc5f0a19-codex-extraction.json").read_text(encoding="utf-8")
    )
    for entry in extraction["entries"]:
        entry["evidence_url"] = "https://www.amazon.com/dp/not-an-asin/ref=tracking"
    extraction_path.write_text(json.dumps(extraction), encoding="utf-8")

    result = _collect(_source(name, extraction_path=extraction_path))

    assert result.source_runs[0]["status"] == "failed"
    reason = result.source_runs[0]["reason"]
    assert "Amazon Charts weekly evidence URL is not a direct Amazon /dp/ product URL" in reason
    assert "20" in reason
    assert "exact ranks 1 through 20" not in reason
    assert result.source_runs[0]["debug_metadata"]["contract_rejected_count"] == 20
    assert result.source_runs[0]["debug_metadata"]["drop_reasons"]


def test_partial_audible_contract_rejection_reports_rule_and_count(tmp_path: Path) -> None:
    name = "Audible Bestselling Computers & Technology Audiobooks"
    extraction = json.loads(
        (RUN_DIR / "audible-bestselling-computers---technology-audiobooks-b6b4652c-codex-extraction.json").read_text(
            encoding="utf-8"
        )
    )
    # Five refused rows of 20 (75% kept) still fail the chart; one would now
    # be dropped and the rest kept (Issue #839, #751 decision C).
    for entry in extraction["entries"][:5]:
        entry["evidence_note"] = "Chart sales were high."
    extraction_path = tmp_path / "computers-invalid-note.json"
    extraction_path.write_text(json.dumps(extraction), encoding="utf-8")

    result = _collect(_source(name, extraction_path=extraction_path))

    assert result.source_runs[0]["status"] == "failed"
    reason = result.source_runs[0]["reason"]
    assert "Audible explicit chart extraction contract rejected 5 entries" in reason
    assert (
        "Audible explicit chart evidence_note must be concise metadata-only without sales, units, reviews, or prose"
        in reason
    )
    assert "expected exact ranks 1 through 20" not in reason
    assert result.source_runs[0]["debug_metadata"]["contract_rejected_count"] == 5
