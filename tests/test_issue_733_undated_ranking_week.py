"""Issue #733: an undated ranking is never asked for a source week.

The ranking-table contract used to require a ``source_week`` "from the
Circana BookScan source statement" from every ranking, including those that
declare ``ranking_reporting_period_undated``.  Their pages state no week, so
the extractor either invented one (Cúspide returned the capture date on
2026-09-20) or returned no rows at all (Cúspide and Bokhandlerforeningen on
2026-09-26).

An undated ranking's week is now the capture's retained observation date,
set in code, as its replay fixtures already did.  The contract no longer asks
for it, and a value the extractor supplies anyway is discarded.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autoscout.source_collectors.agent_mentions import agent_extraction_mentions_from_response
from autoscout.source_collectors.base import with_observed_ranking_week
from autoscout.source_collectors.contract import extraction_contract
from autoscout.source_page_identity import ranking_rows

ROOT = Path(__file__).resolve().parents[1]
BOKHANDLER = "Bokhandlerforeningen Boklista - Generell litteratur voksne"
# The capture the 2026-09-26 weekly run retained for this source, copied
# byte-for-byte: extraction returned no entries from it.
BOKHANDLER_CAPTURE = Path(__file__).resolve().parent / "fixtures" / "issue-733" / "boklista-2026-09-26-capture.json"


def _portfolio_source(name: str) -> dict[str, Any]:
    portfolio = json.loads((ROOT / "configs" / "current-weekly-portfolio.json").read_text(encoding="utf-8"))
    sources = portfolio.get("sources", portfolio) if isinstance(portfolio, dict) else portfolio
    matches = [source for source in sources if source.get("name") == name]
    assert len(matches) == 1, name
    return dict(matches[0])


def _undated_ranking(**extra: Any) -> dict[str, Any]:
    return {
        "name": "Undated Ranking",
        "url": "https://example.test/ranking",
        "metadata_ranking_table": True,
        "ranking_row_selector": ".row",
        "ranking_reporting_period_undated": True,
        "language": "en",
        "eligible_candidate_category": "adult nonfiction",
        **extra,
    }


def _rules_text(contract: dict[str, Any]) -> str:
    return "\n".join(contract["rules"])


@pytest.mark.parametrize(
    "name",
    [BOKHANDLER, "Cúspide 100 más vendidos"],
)
def test_the_configured_undated_rankings_are_not_asked_for_a_week(name: str) -> None:
    source = _portfolio_source(name)
    assert source["ranking_reporting_period_undated"] is True

    contract = extraction_contract(source)
    shape = contract["required_output_shape"]["mentions"][0]

    assert "source_week" not in contract["required_fields"]
    assert "source_week" not in shape
    assert "current_rank" in contract["required_fields"]
    rules = _rules_text(contract)
    assert "Circana" not in rules
    assert "Circana" not in json.dumps(shape)
    assert "states no reporting period" in rules
    assert "dated source_week" not in rules


def test_a_dated_ranking_with_a_configured_statement_still_requires_that_week() -> None:
    source = _undated_ranking(ranking_reporting_period_undated=False, ranking_source_statement="Kilde: Bokhandel")
    contract = extraction_contract(source)

    assert "source_week" in contract["required_fields"]
    shape = contract["required_output_shape"]["mentions"][0]
    assert shape["source_week"] == "Dated week from the configured visible ranking source statement"
    assert "Kilde: Bokhandel" in _rules_text(contract)
    assert "Circana" not in json.dumps(shape)


def test_a_declared_dated_ranking_reads_the_period_the_page_states() -> None:
    contract = extraction_contract(_undated_ranking(ranking_reporting_period_undated=False))

    assert "source_week" in contract["required_fields"]
    shape = contract["required_output_shape"]["mentions"][0]
    assert shape["source_week"] == "Reporting period exactly as the ranking page states it"


def test_a_legacy_circana_table_keeps_its_circana_statement() -> None:
    # A generic ranking table with no declared row selector or statement is
    # grounded against "Source: Circana BookScan", so only it names Circana.
    source = {"name": "PW", "url": "https://example.test/pw", "metadata_ranking_table": True, "language": "en"}
    contract = extraction_contract(source)

    assert "source_week" in contract["required_fields"]
    shape = contract["required_output_shape"]["mentions"][0]
    assert shape["source_week"] == "Dated week from the Circana BookScan source statement"


def test_an_undated_week_is_the_capture_observation_date_not_the_extractor_value() -> None:
    source = _undated_ranking()
    entry = {"title": "Book", "source_week": "2026-09-20"}

    observed = with_observed_ranking_week(entry, source, {"reference_date": "2026-09-26"})
    assert observed["source_week"] == "2026-09-26"
    assert entry["source_week"] == "2026-09-20"

    # Without an observation date the week is dropped, so the required-field
    # check fails closed rather than keeping an invented value.
    assert "source_week" not in with_observed_ranking_week(entry, source, {})


def test_a_dated_ranking_keeps_the_week_the_extractor_read() -> None:
    source = _undated_ranking(ranking_reporting_period_undated=False)
    entry = {"title": "Book", "source_week": "2026-38"}

    assert with_observed_ranking_week(entry, source, {"reference_date": "2026-09-26"}) == entry


def test_the_retained_2026_09_26_boklista_capture_validates_without_a_model_week() -> None:
    source = _portfolio_source(BOKHANDLER)
    capture = json.loads(BOKHANDLER_CAPTURE.read_text(encoding="utf-8"))
    rows = ranking_rows(capture["html"], source)
    assert len(rows) == 26

    # Exactly what the fixed contract asks for: every required field, and no
    # source_week at all.
    entries = [
        {
            "title": row.title,
            "primary_authors": [author.strip() for author in row.author.split(" / ")],
            "isbn": row.identifier,
            "current_rank": int(row.rank),
            "source_url": source["url"],
            "evidence_url": source["url"],
            "signal_type": "bestseller/ranking",
            "signal_strength": "top ranking",
            "evidence_note": f"Rank {row.rank} on the Boklista.",
            "candidate_category": "adult nonfiction",
            "english_title": f"English helper {row.rank}",
            "search_translation": f"Поисковый перевод {row.rank}",
            "author_transliteration_note": "Транслитерация автора",
        }
        for row in rows
    ]
    mentions, dropped = agent_extraction_mentions_from_response(capture, {"entries": entries}, source, "2026-09-26")

    assert dropped == []
    assert len(mentions) == 26
    assert {mention["source_week"] for mention in mentions} == {capture["reference_date"]}
