"""Offline evidence and exact-ISBN fallback tests; never live network tests."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from autoscout.isbn_author_verification import MAX_BYTES
from autoscout.isbn_author_verification import evidence_from_response
from autoscout.isbn_author_verification import lookup_author
from autoscout.isbn_author_verification import query_url
from autoscout.isbn_author_verification import verified_author
from autoscout.source_feasibility import evaluate_source_feasibility
from autoscout.source_page_identity import ranking_rows
from autoscout.source_page_identity import validate_ranking_contract_metadata

ISBN = "9780306406157"
TITLE = "Synthetic Book"


def _xml() -> str:
    return f"""<searchRetrieveResponse xmlns="http://www.loc.gov/zing/srw/">
    <numberOfRecords>1</numberOfRecords><records><record><recordData>
    <record xmlns="http://www.loc.gov/MARC21/slim">
    <controlfield tag="001">1395508348</controlfield>
    <datafield tag="020"><subfield code="a">{ISBN}</subfield><subfield code="c">PRICE</subfield></datafield>
    <datafield tag="100"><subfield code="a">Author, Sample</subfield><subfield code="4">aut</subfield></datafield>
    <datafield tag="245"><subfield code="a">{TITLE}</subfield><subfield code="c">Sample Author</subfield></datafield>
    <datafield tag="520"><subfield code="a">DESCRIPTION</subfield></datafield>
    </record></recordData></record></records></searchRetrieveResponse>"""


def _evidence() -> str:
    return json.dumps([evidence_from_response(_xml(), ISBN, TITLE)])


def _source() -> dict[str, Any]:
    return {
        "url": "https://example.test/list",
        "contract_family": "ranking-table",
        "metadata_ranking_table": True,
        "eligible_candidate_category": "adult nonfiction",
        "ranking_row_selector": ".row",
        "ranking_rank_source": "selector",
        "ranking_rank_selector": ".rank",
        "ranking_title_selector": ".title",
        "ranking_author_selector": ".author",
        "ranking_identifier_selector": ".isbn",
        "ranking_category_marker": "Sachbuch",
        "ranking_reporting_date_selector": ".date",
        "ranking_reporting_period_pattern": r"(?P<period>\d{2}\.\d{2}\.\d{4})",
        "ranking_reporting_period_date_format": "%d.%m.%Y",
        "ranking_reporting_period_max_age_days": 14,
        "ranking_expected_raw_row_count": 1,
        "ranking_evidence_url": "source_page",
        "adult_nonfiction_exclusions": ["Kinderbuch"],
        "ranking_author_evidence_json": _evidence(),
    }


def _html(author: str = "") -> str:
    return (
        '<h1>Sachbuch</h1><div class="date">28.09.2026</div>'
        f'<div class="row"><span class="rank">1</span><h4 class="title">{TITLE}</h4>'
        f'<span class="author">{author}</span><span class="isbn">{ISBN}</span></div>'
    )


def test_exact_isbn_and_title_verified_with_retained_provenance() -> None:
    evidence = evidence_from_response(_xml(), ISBN, TITLE)
    assert evidence["author"] == "Sample Author"
    assert evidence["catalog_author"] == "Author, Sample"
    assert evidence["evidence_url"] == "https://d-nb.info/1395508348"
    assert "DESCRIPTION" not in evidence["record_xml"]
    assert "PRICE" not in evidence["record_xml"]
    assert verified_author(_source(), ISBN, TITLE) == "Sample Author"


def test_fallback_unblocks_missing_author_but_never_overwrites_visible_author() -> None:
    source = _source()
    assert validate_ranking_contract_metadata(source) == ""
    assert ranking_rows(_html(), source)[0].author == "Sample Author"
    assert ranking_rows(_html("Original Author"), source)[0].author == "Original Author"
    capture = {
        "source_page": source["url"],
        "html": _html(),
        "get_status": 200,
        "head_status": 200,
        "reference_date": "2026-10-06",
    }
    assert evaluate_source_feasibility(source, capture).outcome == "accept"
    source.pop("ranking_author_evidence_json")
    assert evaluate_source_feasibility(source, capture).outcome == "reject"


@pytest.mark.parametrize(
    "body",
    [
        _xml().replace(ISBN, "9783462055306"),
        _xml().replace(TITLE, "Different Book"),
        _xml().replace(">aut<", ">edt<"),
        _xml().replace("Sample Author", "edited by Sample Author"),
        _xml().replace("<numberOfRecords>1", "<numberOfRecords>2"),
        "<broken",
        '<!DOCTYPE test [<!ENTITY e "unsafe">]>' + _xml(),
    ],
)
def test_mismatch_ambiguity_and_malformed_records_fail_closed(body: str) -> None:
    with pytest.raises(ValueError):
        evidence_from_response(body, ISBN, TITLE)


@pytest.mark.parametrize(
    "field,value",
    [("author", "Wrong Author"), ("record_sha256", "invalid"), ("evidence_url", "https://other.test/book")],
)
def test_retained_metadata_cannot_disagree_with_record(field: str, value: str) -> None:
    records = json.loads(_evidence())
    records[0][field] = value
    with pytest.raises(ValueError):
        verified_author({"ranking_author_evidence_json": json.dumps(records)}, ISBN, TITLE)


def test_wrong_row_title_is_rejected_and_unknown_isbn_is_not_enriched() -> None:
    with pytest.raises(ValueError, match="title"):
        verified_author(_source(), ISBN, "Different Book")
    assert verified_author(_source(), "9783462055306", TITLE) == ""


def test_invalid_evidence_is_rejected_even_if_no_author_is_missing() -> None:
    source = _source()
    source["ranking_author_evidence_json"] = "broken"
    assert "malformed" in validate_ranking_contract_metadata(source)


def test_lookup_has_one_bounded_request() -> None:
    calls: list[tuple[str, float]] = []

    class Response(io.BytesIO):
        status = 200

        def geturl(self) -> str:
            return query_url(ISBN)

        def read(self, size: int = -1) -> bytes:
            assert size == MAX_BYTES + 1
            return super().read(size)

    def opener(request: Any, timeout: float) -> Response:
        calls.append((request.full_url, timeout))
        return Response(_xml().encode())

    assert lookup_author(ISBN, TITLE, opener=opener)["author"] == "Sample Author"
    assert calls == [(query_url(ISBN), 15)]


def test_invalid_isbn_is_rejected_before_network() -> None:
    def opener(*args: Any, **kwargs: Any) -> None:
        pytest.fail("must not access network")

    with pytest.raises(ValueError, match="checksum"):
        lookup_author("9780306406158", TITLE, opener=opener)


@pytest.mark.parametrize("value", [None, False, 0, "", " "])
def test_present_evidence_must_be_nonblank_json_text(value: Any) -> None:
    with pytest.raises(ValueError):
        verified_author({"ranking_author_evidence_json": value}, ISBN, TITLE)


def test_oversized_response_fails_closed() -> None:
    class Response(io.BytesIO):
        status = 200

        def geturl(self) -> str:
            return query_url(ISBN)

    def opener(request: Any, timeout: float) -> Response:
        return Response(b"x" * (MAX_BYTES + 1))

    with pytest.raises(ValueError, match="capture limit"):
        lookup_author(ISBN, TITLE, opener=opener)
