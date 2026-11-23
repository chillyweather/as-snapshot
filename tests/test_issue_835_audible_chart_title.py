"""Issue #835 — an Audible title joined with its subtitle keeps the chart's title.

In the second 2026-10-15 weekly run the extractor returned rank 1 of the
Science & Engineering chart as "Protocols An Operating Manual for the Human
Body". The chart entry's link text is "Protocols"; the product container
shows the subtitle in its own element. The grounding check requires the link
text exactly, so 19 of 20 rows were dropped and the source failed.

Fixtures, from the retained run of operation 4b8f822e:

- ``issue-835-audible-chart.html``: the 20 chart entries of the capture. Each
  product container keeps the live markup of the parts the parsers read: the
  ranked title link, the subtitle, and the author line.
- ``issue-835-audible-chart-extraction.json``: that run's extraction, verbatim.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urljoin

import pytest

from autoscout.source_collectors.agent_mentions import agent_extraction_mentions_from_response
from autoscout.source_collectors.contract import extraction_contract
from autoscout.source_page_identity import audible_chart_entry_title
from autoscout.source_page_identity import audible_chart_mention_is_grounded

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
HTML = (FIXTURES / "issue-835-audible-chart.html").read_text(encoding="utf-8")
EXTRACTION = json.loads((FIXTURES / "issue-835-audible-chart-extraction.json").read_text(encoding="utf-8"))
SOURCE_PAGE = "https://www.audible.com/charts/best/science-engineering-audiobooks/18580540011"
SOURCE_NAME = "Audible Bestselling Science & Engineering Audiobooks"
PRODUCT_PATH = (
    "/pd/Protocols-Audiobook/B0D6828XPN?ref_pageloadid=not_applicable"
    "&pf_rd_p=32bba03a-26cd-491a-baa2-b83ff0238e68&pf_rd_r=HFSYP9NZ8M02HG4676ED"
    "&plink=1E5OpFntfmgI3Mps&pageLoadId=tYZpI0SQoRkEQvA0"
    "&creativeId=d3265378-de1c-4d52-95d5-f373fb9f47da&ref=a_charts_be_c4_adblp13nchpl_1_1"
)
PRODUCT_URL = urljoin(SOURCE_PAGE, PRODUCT_PATH)
JOINED_TITLE = "Protocols An Operating Manual for the Human Body"


def _source() -> dict:
    portfolio = json.loads((ROOT / "configs" / "current-weekly-portfolio.json").read_text(encoding="utf-8"))
    return next(source for source in portfolio["sources"] if source["name"] == SOURCE_NAME)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        (JOINED_TITLE, "Protocols"),
        ("Protocols: An Operating Manual for the Human Body", "Protocols"),
        ("Protocols", "Protocols"),
        # Text the product container does not show is not a subtitle.
        ("Protocols A Manual Nobody Printed", "Protocols A Manual Nobody Printed"),
        # A different title is left for the grounding check to refuse.
        ("Sapiens A Brief History of Humankind", "Sapiens A Brief History of Humankind"),
        ("Protocolsx An Operating Manual for the Human Body", "Protocolsx An Operating Manual for the Human Body"),
    ],
)
def test_only_the_chart_title_followed_by_its_own_subtitle_is_shortened(title: str, expected: str) -> None:
    assert audible_chart_entry_title(HTML, SOURCE_PAGE, title=title, evidence_url=PRODUCT_URL) == expected


def test_a_title_for_another_product_url_is_unchanged() -> None:
    other_url = PRODUCT_URL.replace("B0D6828XPN", "B0FY53N9BD")

    assert audible_chart_entry_title(HTML, SOURCE_PAGE, title=JOINED_TITLE, evidence_url=other_url) == JOINED_TITLE


def test_the_grounding_rule_itself_is_unchanged() -> None:
    """The joined title still does not ground on its own; only the stored title changes."""
    kwargs = {"rank": 1, "authors": ["Andrew D. Huberman Ph.D."], "evidence_url": PRODUCT_URL}

    assert not audible_chart_mention_is_grounded(HTML, SOURCE_PAGE, title=JOINED_TITLE, **kwargs)
    assert audible_chart_mention_is_grounded(HTML, SOURCE_PAGE, title="Protocols", **kwargs)


def _extraction_with_rank_one(title: str) -> dict:
    entries = [dict(entry) for entry in EXTRACTION["entries"]]
    entries[0]["title"] = title
    return {**EXTRACTION, "entries": entries}


def test_the_retained_extraction_is_collected_in_full_under_the_chart_titles() -> None:
    """The run that lost 19 of 20 rows now keeps all 20, with the chart's titles."""
    page = {"source_page": SOURCE_PAGE, "html": HTML}

    mentions, dropped = agent_extraction_mentions_from_response(page, EXTRACTION, _source(), "2026-10-15")

    assert dropped == []
    assert len(mentions) == 20
    assert mentions[0]["title"] == "Protocols"
    assert mentions[3]["title"] == "Sapiens"
    # A title whose colon is part of the link text is kept whole.
    assert "The Sleeping World: Exploring Floating Gardens with an Axolotl" in [m["title"] for m in mentions]


def test_a_row_with_an_invented_subtitle_is_still_refused() -> None:
    """Text the product container does not show is not a subtitle; the row is
    refused as before this issue. Since #839 the chart keeps its other 19 rows."""
    page = {"source_page": SOURCE_PAGE, "html": HTML}

    mentions, dropped = agent_extraction_mentions_from_response(
        page, _extraction_with_rank_one("Protocols A Manual Nobody Printed"), _source(), "2026-10-15"
    )

    assert len(mentions) == 19
    assert [drop["title"] for drop in dropped] == ["Protocols A Manual Nobody Printed"]
    assert dropped[0]["reason"].startswith("Audible explicit chart rank, title")


def test_the_contract_tells_the_extractor_to_leave_the_subtitle_out() -> None:
    contract = extraction_contract(_source())

    assert any("never append the subtitle" in rule for rule in contract["rules"])
