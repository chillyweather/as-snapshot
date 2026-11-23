"""Offline acceptance tests for Issue #587 ranking declaration evidence."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from autoscout.__main__ import main
from autoscout.http_decoding import MAX_RESPONSE_BYTES
from autoscout.source_probe import SourceCandidateLedgerError
from autoscout.source_probe import read_source_candidate_ledger
from autoscout.source_probe import run_source_candidate_probe

SOURCE_URL = "https://example.test/aladin-ranking"


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"", url: str = SOURCE_URL) -> None:
        self.status = status
        self.body = body
        self.url = url

    def read(self, _size: int) -> bytes:
        return self.body

    def geturl(self) -> str:
        return self.url

    def close(self) -> None:
        return None


class FakeTransport:
    def __init__(self, body: bytes) -> None:
        self.responses = [FakeResponse(200), FakeResponse(200, body)]

    def __call__(self, _request: Any, timeout: float) -> FakeResponse:
        return self.responses.pop(0)


class NoNetwork:
    def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("network access was attempted")


class RequestFailure:
    def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        raise OSError("synthetic request failure")


def _ranking_metadata() -> dict[str, Any]:
    return {
        "ranking_rank_source": "document_order",
        "ranking_row_selector": ".item",
        "ranking_title_selector": ".title",
        "ranking_author_selector": ".author",
        "ranking_identifier_attribute": "data-item-id",
        "ranking_category_marker": "베스트셀러",
        "ranking_reporting_period_pattern": r"(?P<period>\d{4}-\d{2}-\d{2}\s*-\s*\d{4}-\d{2}-\d{2})",
        "ranking_reporting_period_date_format": "%Y-%m-%d",
        "ranking_reporting_period_max_age_days": 14,
        "ranking_expected_raw_row_count": 1,
        "ranking_evidence_url": "source_page",
        "adult_nonfiction_exclusions": ["fiction"],
        "metadata_ranking_table": True,
    }


def _probe_html() -> bytes:
    period = "13/08/2026 - 19/08/2026"
    return f"""
    <html><body>
      <nav>Navigation ISBN 9780306406157</nav>
      <h1>알라딘 베스트셀러 {period}</h1>
      <div class="item" data-item-id="123456789">
        <span class="title">Visible title</span>
        <span class="isbn">9780306406157</span>
      </div>
    </body></html>
    """.encode()


def _probe(
    tmp_path: Path,
    html: bytes,
    *,
    metadata: dict[str, Any] | None = None,
    opener: Any | None = None,
    ledger: Path | None = None,
) -> tuple[dict[str, Any], int]:
    return run_source_candidate_probe(
        [SOURCE_URL],
        contract_families=["ranking-table"],
        contract_metadata=[metadata or _ranking_metadata()],
        ledger_path=ledger or tmp_path / "ledger.json",
        feasibility_dir=tmp_path / "drafts",
        opener=opener or FakeTransport(html),
    )


def test_rejected_ranking_probe_reports_independent_calendar_and_identifier_evidence(tmp_path: Path) -> None:
    html = _probe_html()
    period = "13/08/2026 - 19/08/2026"

    report, code = _probe(tmp_path, html)

    assert code == 1
    result = report["candidates"][0]
    assert result["outcome"] == "reject"
    assert result["ranking_declaration_diagnostics"] == {
        "reporting_period": {
            "status": "calendar_date_range",
            "candidates": [
                {
                    "text": period,
                    "pattern": r"(?P<period>\d{1,2}/\d{1,2}/\d{4}\s*-\s*\d{1,2}/\d{1,2}/\d{4})",
                    "date_formats": ["%d/%m/%Y"],
                }
            ],
        },
        "identifier_candidates": [
            {
                "kind": "internal_item_id",
                "source": "attribute",
                "tag": "div",
                "class_tokens": ["item"],
                "attribute": "data-item-id",
                "value_shape": "digits:9",
            },
            {
                "kind": "isbn",
                "source": "text",
                "tag": "span",
                "class_tokens": ["isbn"],
                "attribute": "",
                "value_shape": "isbn-13",
            },
        ],
    }
    assert "Navigation" not in str(result["ranking_declaration_diagnostics"])
    assert "Visible title" not in str(result["ranking_declaration_diagnostics"])
    assert "123456789" not in str(result["ranking_declaration_diagnostics"])
    assert "9780306406157" not in str(result["ranking_declaration_diagnostics"])


@pytest.mark.parametrize(
    "heading",
    [
        "<main><section><h1>Ranking 13/08/2026 - 19/08/2026</h1></section></main>",
        '<h1 class="page-title">Ranking 13/08/2026 - 19/08/2026</h1>',
    ],
)
def test_semantic_page_heading_with_page_title_class_provides_period_diagnostics(tmp_path: Path, heading: str) -> None:
    html = f"""
    <html><body>
      {heading}
    </body></html>
    """.encode()
    metadata = _ranking_metadata()
    metadata["ranking_category_marker"] = "Ranking"
    metadata["ranking_reporting_period_pattern"] = r"(?P<period>wrong)"

    report, code = _probe(tmp_path, html, metadata=metadata)

    assert code == 1
    assert report["candidates"][0]["ranking_declaration_diagnostics"]["reporting_period"] == {
        "status": "calendar_date_range",
        "candidates": [
            {
                "text": "13/08/2026 - 19/08/2026",
                "pattern": r"(?P<period>\d{1,2}/\d{1,2}/\d{4}\s*-\s*\d{1,2}/\d{1,2}/\d{4})",
                "date_formats": ["%d/%m/%Y"],
            }
        ],
    }


@pytest.mark.parametrize(
    ("fragment", "expected_status"),
    [
        ('<div><span class="foo">2026.08.01 ~ 2026.08.07</span></div>', "calendar_date_range"),
        ('<span class="ss_book_list">2026년 8월 3주</span>', "week_ordinal"),
        ("<div hidden>2026.08.01 ~ 2026.08.07</div>", "not_found"),
        ("<nav>2026.08.01 ~ 2026.08.07</nav>", "not_found"),
        ('<div class="book-row"><span>2026.08.01 ~ 2026.08.07</span></div>', "not_found"),
    ],
)
def test_period_diagnostics_are_class_agnostic_with_privacy_boundaries(
    tmp_path: Path, fragment: str, expected_status: str
) -> None:
    html = f"<html><body>{fragment}</body></html>".encode()
    metadata = _ranking_metadata()
    metadata["ranking_category_marker"] = "ranking"
    metadata["ranking_reporting_period_pattern"] = r"(?P<period>wrong)"

    report, code = _probe(tmp_path, html, metadata=metadata)

    assert code == 1
    reporting_period = report["candidates"][0]["ranking_declaration_diagnostics"]["reporting_period"]
    assert reporting_period["status"] == expected_status
    if expected_status == "calendar_date_range":
        assert reporting_period["candidates"] == [
            {
                "text": "2026.08.01 ~ 2026.08.07",
                "pattern": r"(?P<period>\d{4}\.\d{1,2}\.\d{1,2}\s*\~\s*\d{4}\.\d{1,2}\.\d{1,2})",
                "date_formats": ["%Y.%m.%d"],
            }
        ]
    else:
        assert reporting_period["candidates"] == []


def test_book_row_and_candidate_contexts_do_not_supply_period_diagnostics(tmp_path: Path) -> None:
    html = b"""
    <html><body>
      <div class="book-row"><h2>Book title 13/08/2026 - 19/08/2026</h2></div>
      <div class="candidate">Candidate title 13/08/2026 - 19/08/2026</div>
    </body></html>
    """
    metadata = _ranking_metadata()
    metadata["ranking_category_marker"] = "ranking"
    metadata["ranking_reporting_period_pattern"] = r"(?P<period>wrong)"

    report, code = _probe(tmp_path, html, metadata=metadata)

    assert code == 1
    assert report["candidates"][0]["ranking_declaration_diagnostics"]["reporting_period"] == {
        "status": "not_found",
        "candidates": [],
    }


def test_excluded_content_cannot_supply_period_diagnostics(tmp_path: Path) -> None:
    html = """
    <html><body>
      <h1>Aladin ranking</h1>
      <nav>03/08/2026 - 09/08/2026</nav>
      <header>2026년 8월 3주</header>
      <article class="review-body">
        <h2>Review 03/08/2026 - 09/08/2026</h2>
        <p class="book-title">Author Name 2026년 8월 3주</p>
      </article>
      <div class="ranking-row"><span class="title">No evidence row</span></div>
    </body></html>
    """.encode()
    metadata = _ranking_metadata()
    metadata["ranking_category_marker"] = "ranking"
    metadata["ranking_reporting_period_pattern"] = r"(?P<period>wrong)"

    report, code = _probe(tmp_path, html, metadata=metadata)

    assert code == 1
    assert report["candidates"][0]["ranking_declaration_diagnostics"]["reporting_period"] == {
        "status": "not_found",
        "candidates": [],
    }


def test_probe_source_candidate_cli_emits_week_ordinal_diagnostics(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    html = "<html><body><h1>알라딘 베스트셀러 2026년 8월 3주</h1><div class='item'><span class='title'>Title</span></div></body></html>".encode()
    monkeypatch.setattr("autoscout.source_probe.urlopen", FakeTransport(html))

    code = main(
        [
            "probe-source-candidate",
            "--url",
            SOURCE_URL,
            "--contract-family",
            "ranking-table",
            "--ranking-row-selector",
            ".item",
            "--ranking-rank-source",
            "document_order",
            "--ranking-title-selector",
            ".title",
            "--ranking-author-selector",
            ".author",
            "--ranking-identifier-attribute",
            "data-item-id",
            "--ranking-category-marker",
            "베스트셀러",
            "--ranking-reporting-period-pattern",
            r"(?P<period>\d{4}-\d{2}-\d{2})",
            "--ranking-reporting-period-date-format",
            "%Y-%m-%d",
            "--ranking-reporting-period-max-age-days",
            "14",
            "--ranking-expected-raw-row-count",
            "1",
            "--ranking-evidence-url",
            "source_page",
            "--ranking-exclusion-term",
            "fiction",
            "--ledger",
            str(tmp_path / "ledger.json"),
            "--feasibility-dir",
            str(tmp_path / "drafts"),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert code == 1
    assert report["candidates"][0]["ranking_declaration_diagnostics"]["reporting_period"] == {
        "status": "week_ordinal",
        "candidates": [],
    }


def test_ranking_declaration_diagnostics_round_trip_through_v3_ledger_history(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    first_report, first_code = _probe(tmp_path, _probe_html(), ledger=ledger)
    expected = first_report["candidates"][0]["ranking_declaration_diagnostics"]
    assert first_code == 1
    assert read_source_candidate_ledger(ledger)[SOURCE_URL]["ranking_declaration_diagnostics"] == expected

    second_report, second_code = _probe(
        tmp_path,
        _probe_html(),
        ledger=ledger,
        opener=FakeTransport(_probe_html()),
    )
    assert second_code == 1
    assert second_report["candidates"][0]["reason_code"] == "already_evaluated"

    third_report, third_code = run_source_candidate_probe(
        [SOURCE_URL],
        contract_families=["ranking-table"],
        contract_metadata=[_ranking_metadata()],
        ledger_path=ledger,
        feasibility_dir=tmp_path / "drafts",
        re_probe=True,
        opener=FakeTransport(_probe_html()),
    )
    assert third_code == 1
    assert third_report["candidates"][0]["ranking_declaration_diagnostics"] == expected
    stored = read_source_candidate_ledger(ledger)[SOURCE_URL]
    assert stored["ranking_declaration_diagnostics"] == expected
    assert stored["prior_verdicts"][0]["ranking_declaration_diagnostics"] == expected


def test_nested_safe_elements_deduplicate_periods_and_keep_four_candidate_bound(tmp_path: Path) -> None:
    periods = [f"2026-08-{day:02d} - 2026-08-{day + 1:02d}" for day in range(1, 7)]
    fragments = "".join(f'<div>{period}<span class="foo">{period}</span></div>' for period in periods)
    metadata = _ranking_metadata()
    metadata["ranking_category_marker"] = "베스트셀러"
    metadata["ranking_reporting_period_pattern"] = r"(?P<period>wrong)"

    report, code = _probe(tmp_path, f"<html><body>{fragments}</body></html>".encode(), metadata=metadata)

    assert code == 1
    candidates = report["candidates"][0]["ranking_declaration_diagnostics"]["reporting_period"]["candidates"]
    assert [candidate["text"] for candidate in candidates] == periods[:4]
    assert len(candidates) == 4


def test_ambiguous_day_month_period_reports_both_valid_formats(tmp_path: Path) -> None:
    html = "<html><body><h1>베스트셀러 01/02/2026 - 03/04/2026</h1></body></html>".encode()
    metadata = _ranking_metadata()
    metadata["ranking_category_marker"] = "베스트셀러"
    metadata["ranking_reporting_period_pattern"] = r"(?P<period>wrong)"

    report, code = _probe(tmp_path, html, metadata=metadata)

    assert code == 1
    candidates = report["candidates"][0]["ranking_declaration_diagnostics"]["reporting_period"]["candidates"]
    assert candidates[0]["date_formats"] == ["%d/%m/%Y", "%m/%d/%Y"]


@pytest.mark.parametrize(
    ("period", "expected_formats", "expected_pattern", "later_period"),
    [
        (
            "2026.8.1~2026.8.7",
            ["%Y.%m.%d"],
            r"(?P<period>\d{4}\.\d{1,2}\.\d{1,2}\s*\~\s*\d{4}\.\d{1,2}\.\d{1,2})",
            "2026.08.01~2026.08.07",
        ),
        (
            "13/8/2026~19/8/2026",
            ["%d/%m/%Y"],
            r"(?P<period>\d{1,2}/\d{1,2}/\d{4}\s*\~\s*\d{1,2}/\d{1,2}/\d{4})",
            "13/08/2026~19/08/2026",
        ),
    ],
)
def test_non_padded_period_patterns_match_later_padded_dates(
    tmp_path: Path,
    period: str,
    expected_formats: list[str],
    expected_pattern: str,
    later_period: str,
) -> None:
    html = f'<html><body><span class="foo">{period}</span></body></html>'.encode()
    metadata = _ranking_metadata()
    metadata["ranking_category_marker"] = "베스트셀러"
    metadata["ranking_reporting_period_pattern"] = r"(?P<period>wrong)"

    report, code = _probe(tmp_path, html, metadata=metadata)

    assert code == 1
    candidate = report["candidates"][0]["ranking_declaration_diagnostics"]["reporting_period"]["candidates"][0]
    assert candidate == {"text": period, "pattern": expected_pattern, "date_formats": expected_formats}
    assert re.fullmatch(candidate["pattern"], period)
    assert re.fullmatch(candidate["pattern"], later_period)


def test_existing_a_period_connector_gets_a_ready_named_pattern(tmp_path: Path) -> None:
    html = "<html><body><h1>베스트셀러 03/08/2026 a 09/08/2026</h1></body></html>".encode()
    metadata = _ranking_metadata()
    metadata["ranking_category_marker"] = "베스트셀러"
    metadata["ranking_reporting_period_pattern"] = r"(?P<period>wrong)"

    report, code = _probe(tmp_path, html, metadata=metadata)

    assert code == 1
    candidate = report["candidates"][0]["ranking_declaration_diagnostics"]["reporting_period"]["candidates"][0]
    assert candidate["text"] == "03/08/2026 a 09/08/2026"
    assert re.search(candidate["pattern"], candidate["text"])
    assert candidate["date_formats"] == ["%d/%m/%Y", "%m/%d/%Y"]


@pytest.mark.parametrize(
    "period",
    [
        "03/08/2026 - 09-08-2026",
        "2026-08-03 - 09/08/2026",
        "103/08/2026 - 09/08/2026",
        "31/02/2026 - 05/03/2026",
        "09/08/2026 - 03/08/2026",
    ],
)
def test_malformed_or_reversed_periods_do_not_become_calendar_evidence(tmp_path: Path, period: str) -> None:
    html = f"<html><body><h1>베스트셀러 {period}</h1></body></html>".encode()
    metadata = _ranking_metadata()
    metadata["ranking_category_marker"] = "베스트셀러"
    metadata["ranking_reporting_period_pattern"] = r"(?P<period>wrong)"

    report, code = _probe(tmp_path, html, metadata=metadata)

    assert code == 1
    assert report["candidates"][0]["ranking_declaration_diagnostics"]["reporting_period"] == {
        "status": "not_found",
        "candidates": [],
    }


def test_no_period_and_hidden_evidence_are_not_reported(tmp_path: Path) -> None:
    html = b"""
    <html><body>
      <h1>Aladin ranking</h1>
      <div hidden>2026-08-01 - 2026-08-08 9780306406157</div>
      <div aria-hidden="true" data-item-id="hidden-123"><span data-isbn="9780140328721">Hidden ISBN</span></div>
      <div class="item"><span class="title">Title text</span><span class="author">Author text</span>
        <p>Long prose description and review body.</p>
      </div>
    </body></html>
    """
    metadata = _ranking_metadata()
    metadata["ranking_category_marker"] = "ranking"

    report, code = _probe(tmp_path, html, metadata=metadata)

    assert code == 1
    diagnostics = report["candidates"][0]["ranking_declaration_diagnostics"]
    assert diagnostics == {
        "reporting_period": {"status": "not_found", "candidates": []},
        "identifier_candidates": [],
    }
    diagnostics_text = json.dumps(diagnostics, ensure_ascii=False)
    for forbidden in ("Title text", "Author text", "description", "review", "9780306406157", "hidden-123"):
        assert forbidden not in diagnostics_text


def test_hidden_void_element_attributes_do_not_produce_identifier_candidates(tmp_path: Path) -> None:
    html = b"""
    <html><body>
      <h1>Aladin ranking</h1>
      <input hidden data-item-id="12345">
      <img hidden data-book-id="67890">
      <source hidden data-product-id="abc123">
    </body></html>
    """
    metadata = _ranking_metadata()
    metadata["ranking_category_marker"] = "ranking"
    metadata["ranking_reporting_period_pattern"] = r"(?P<period>wrong)"

    report, code = _probe(tmp_path, html, metadata=metadata)

    assert code == 1
    assert report["candidates"][0]["ranking_declaration_diagnostics"]["identifier_candidates"] == []


def test_duplicate_identifier_classes_are_canonical_and_persisted(tmp_path: Path) -> None:
    html = b"""
    <html><body>
      <h1>Aladin ranking 13/08/2026 - 19/08/2026</h1>
      <div class="item item extra extra third" data-item-id="123456789">
        <span class="title">Title</span>
      </div>
    </body></html>
    """
    metadata = _ranking_metadata()
    metadata["ranking_category_marker"] = "ranking"
    metadata["ranking_reporting_period_pattern"] = r"(?P<period>wrong)"
    ledger = tmp_path / "ledger.json"

    report, code = _probe(tmp_path, html, metadata=metadata, ledger=ledger)

    assert code == 1
    expected = {
        "kind": "internal_item_id",
        "source": "attribute",
        "tag": "div",
        "class_tokens": ["item", "extra", "third"],
        "attribute": "data-item-id",
        "value_shape": "digits:9",
    }
    assert report["candidates"][0]["ranking_declaration_diagnostics"]["identifier_candidates"] == [expected]
    assert read_source_candidate_ledger(ledger)[SOURCE_URL]["ranking_declaration_diagnostics"][
        "identifier_candidates"
    ] == [expected]


def test_diagnostics_have_deterministic_period_identifier_and_class_bounds(tmp_path: Path) -> None:
    periods = [f"2026-08-{day:02d} - 2026-08-{day + 1:02d}" for day in range(1, 6)]
    isbn_values = ["9780306406157", "9780140328721", "9781401971366"]
    headings = "".join(f"<h1>베스트셀러 {period}</h1>" for period in periods)
    rows = "".join(
        f'<div class="item-{index} extra-{index} third-{index} fourth-{index} fifth-{index}" '
        f'data-item-id="{index + 100000}"><span class="isbn-{index}">{isbn_values[index % 3]}</span></div>'
        for index in range(9)
    )
    html = f"<html><body>{headings}{rows}</body></html>".encode()
    metadata = _ranking_metadata()
    metadata["ranking_category_marker"] = "베스트셀러"
    metadata["ranking_reporting_period_pattern"] = r"(?P<period>wrong)"

    report, code = _probe(tmp_path, html, metadata=metadata)

    assert code == 1
    diagnostics = report["candidates"][0]["ranking_declaration_diagnostics"]
    assert len(diagnostics["reporting_period"]["candidates"]) == 4
    assert len(diagnostics["identifier_candidates"]) == 8
    assert all(len(candidate["class_tokens"]) <= 4 for candidate in diagnostics["identifier_candidates"])
    assert diagnostics == report["candidates"][0]["ranking_declaration_diagnostics"]


def test_sanitizer_rejects_old_observed_width_pattern_before_network(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    html = b'<html><body><span class="foo">2026.8.1~2026.8.7</span></body></html>'
    _probe(tmp_path, html, ledger=ledger)
    data = json.loads(ledger.read_text(encoding="utf-8"))
    data["entries"][0]["ranking_declaration_diagnostics"]["reporting_period"]["candidates"][0]["pattern"] = (
        r"(?P<period>\d{4}\.\d{1}\.\d{1}\s*\~\s*\d{4}\.\d{1}\.\d{1})"
    )
    ledger.write_text(json.dumps(data), encoding="utf-8")
    original = ledger.read_bytes()

    with pytest.raises(SourceCandidateLedgerError, match="ranking_declaration_diagnostics"):
        run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, opener=NoNetwork())
    assert ledger.read_bytes() == original


def test_request_failure_without_html_has_no_diagnostics(tmp_path: Path) -> None:
    report, code = _probe(tmp_path, b"", opener=RequestFailure())

    assert code == 1
    assert report["candidates"][0]["reason_code"] == "head_request_failed"
    assert "ranking_declaration_diagnostics" not in report["candidates"][0]


def test_declaration_diagnostics_are_omitted_for_accept_generic_and_truncated_results(tmp_path: Path) -> None:
    accepted_metadata = _ranking_metadata()
    accepted_metadata.update(
        {
            "ranking_identifier_attribute": "",
            "ranking_identifier_selector": ".isbn",
            "ranking_reporting_period_undated": True,
        }
    )
    accepted_metadata.pop("ranking_reporting_period_pattern")
    accepted_metadata.pop("ranking_reporting_period_date_format")
    accepted_metadata.pop("ranking_reporting_period_max_age_days")
    accepted_html = _probe_html().replace(
        b'<span class="title">Visible title</span>',
        b'<span class="title">Visible title</span><span class="author">Visible author</span>',
    )
    accepted_report, accepted_code = _probe(tmp_path, accepted_html, metadata=accepted_metadata)
    assert accepted_code == 0
    assert "ranking_declaration_diagnostics" not in accepted_report["candidates"][0]

    generic_html = b"<html><body><h1>Current list</h1><ul><li><a href='/book'>Book</a><span>By: Author</span></li></ul></body></html>"
    generic_report, generic_code = run_source_candidate_probe(
        [SOURCE_URL],
        contract_families=["generic"],
        ledger_path=tmp_path / "generic.json",
        feasibility_dir=tmp_path / "generic-drafts",
        opener=FakeTransport(generic_html),
    )
    assert generic_code == 0
    assert "ranking_declaration_diagnostics" not in generic_report["candidates"][0]

    truncated_report, truncated_code = _probe(
        tmp_path,
        b"<html>" + b"x" * MAX_RESPONSE_BYTES,
        ledger=tmp_path / "truncated.json",
    )
    assert truncated_code == 3
    assert "ranking_declaration_diagnostics" not in truncated_report["candidates"][0]


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_field",
        "overbound",
        "unsafe_tag",
        "unsafe_class",
        "invalid_status",
        "invalid_pattern",
        "invalid_format",
        "duplicate",
    ],
)
def test_malformed_v3_diagnostics_are_rejected_before_network(tmp_path: Path, mutation: str) -> None:
    ledger = tmp_path / "ledger.json"
    _probe(tmp_path, _probe_html(), ledger=ledger)
    data = json.loads(ledger.read_text(encoding="utf-8"))
    diagnostics = data["entries"][0]["ranking_declaration_diagnostics"]
    period_candidate = diagnostics["reporting_period"]["candidates"][0]
    identifier_candidate = diagnostics["identifier_candidates"][0]
    if mutation == "unknown_field":
        diagnostics["unexpected"] = "value"
    elif mutation == "overbound":
        diagnostics["identifier_candidates"] = diagnostics["identifier_candidates"] * 5
    elif mutation == "unsafe_tag":
        identifier_candidate["tag"] = "article"
    elif mutation == "unsafe_class":
        identifier_candidate["class_tokens"] = ["review-body"]
    elif mutation == "invalid_status":
        diagnostics["reporting_period"]["status"] = "calendar"
    elif mutation == "invalid_pattern":
        period_candidate["pattern"] = "(?P<period>prose)"
    elif mutation == "invalid_format":
        period_candidate["date_formats"] = ["%Y-%m-%d"]
    else:
        diagnostics["reporting_period"]["candidates"].append(dict(period_candidate))
    ledger.write_text(json.dumps(data), encoding="utf-8")
    original = ledger.read_bytes()

    with pytest.raises(SourceCandidateLedgerError, match="ranking_declaration_diagnostics"):
        run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, opener=NoNetwork())
    assert ledger.read_bytes() == original


def _legacy_entry(url: str, *, version: int) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "normalized_url": url,
        "probe_date": "2026-08-25",
        "url": url,
        "name": "Example",
        "signal_family": "award",
        "outcome": "reject",
        "reason_code": "reason",
        "reason": "reason",
        "http_evidence": {
            "head": {"status": 200, "resolved_url": url, "error": ""},
            "get": {"status": 200, "resolved_url": url, "body_bytes": 10, "truncated": False},
        },
        "head_status": 200,
        "get_status": 200,
        "resolved_url": url,
        "detected_heading": "",
        "bounded_entry_count": 0,
        "samples": [],
    }
    if version >= 2:
        entry["proposing_context"] = "test"
    if version >= 3:
        entry["contract_family"] = "generic"
    return entry


def test_version_1_2_and_3_ledgers_remain_read_compatible(tmp_path: Path) -> None:
    for version in (1, 2, 3):
        url = f"https://example.test/legacy-{version}"
        entry = _legacy_entry(url, version=version)
        if version == 1:
            payload: dict[str, Any] = {"version": version, "entries": [entry]}
        else:
            payload = {
                "version": version,
                "accept_count": 0,
                "defer_count": 0,
                "reject_count": 1,
                "total_entry_count": 1,
                "entries": [entry],
            }
        path = tmp_path / f"v{version}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        loaded = read_source_candidate_ledger(path)
        assert loaded[url]["url"] == url
        if version == 1:
            assert loaded[url]["proposing_context"] == "legacy-v1-unrecorded"
        else:
            assert loaded[url]["proposing_context"] == "test"


def test_an_isbn_on_an_attribute_is_reported_as_an_isbn(tmp_path: Path) -> None:
    """A retailer that carries the ISBN only on an attribute still gets an answer.

    The attribute allowlist holds ``isbn`` and ``data-isbn`` because a row
    commonly carries the identifier there and nowhere in its text. Reporting no
    identifier for such a row is the wrong answer this issue exists to remove,
    and it is indistinguishable from a row that truly has none.
    """
    html = b"""
    <html><body>
      <h1>Aladin ranking</h1>
      <ol>
        <li class="row" data-isbn="9780099590088"><span class="rank">1</span></li>
        <li class="row" data-isbn="0140449132"><span class="rank">2</span></li>
        <li class="row" data-item-id="AB12345678"><span class="rank">3</span></li>
      </ol>
    </body></html>
    """
    metadata = _ranking_metadata()
    metadata["ranking_category_marker"] = "ranking"
    metadata["ranking_reporting_period_pattern"] = r"(?P<period>wrong)"

    report, code = _probe(tmp_path, html, metadata=metadata)

    assert code == 1
    candidates = report["candidates"][0]["ranking_declaration_diagnostics"]["identifier_candidates"]
    by_shape = {candidate["value_shape"]: candidate for candidate in candidates}

    # Both ISBN widths are named as ISBNs, from the attribute rather than the text.
    for shape in ("isbn-13", "isbn-10"):
        assert shape in by_shape, f"{shape} missing from {candidates}"
        assert by_shape[shape]["kind"] == "isbn"
        assert by_shape[shape]["source"] == "attribute"
        assert by_shape[shape]["attribute"] == "data-isbn"

    # A non-ISBN value on an allowlisted attribute stays an internal item id.
    assert by_shape["alphanumeric:10"]["kind"] == "internal_item_id"

    # The value itself never reaches the ledger, only its shape.
    serialized = json.dumps(report, ensure_ascii=False)
    for value in ("9780099590088", "0140449132", "AB12345678"):
        assert value not in serialized
