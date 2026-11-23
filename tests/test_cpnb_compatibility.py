"""CPNB opt-in HEAD exception and grounded publication-week freshness."""

from datetime import date
from io import BytesIO
from typing import ClassVar
from urllib.error import HTTPError

import pytest

from autoscout.source_feasibility import evaluate_source_feasibility
from autoscout.source_page_identity import ranking_page_identity
from autoscout.source_page_identity import validate_ranking_contract_metadata
from autoscout.source_probe import run_source_candidate_probe

URL = "https://example.test/non-fictie"
REFERENCE_DATE = date(2026, 10, 6)


@pytest.fixture(autouse=True)
def fixed_probe_date(monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return REFERENCE_DATE

    monkeypatch.setattr("autoscout.source_probe.date", FixedDate)


def source():
    return {
        "url": URL,
        "contract_family": "ranking-table",
        "metadata_ranking_table": True,
        "visible_title_selector": ".board__title",
        "ranking_head_404_get_fallback": True,
        "ranking_row_selector": ".card",
        "ranking_rank_source": "selector",
        "ranking_rank_selector": ".card__position",
        "ranking_title_selector": ".card__title",
        "ranking_author_selector": ".card__author",
        "ranking_identifier_pattern": r"ISBN\s+(?P<identifier>97[89][0-9]{10})",
        "ranking_category_marker": "Top Non-Fictie",
        "ranking_reporting_date_selector": ".board__week",
        "ranking_reporting_period_pattern": r"(?P<period>Week [0-9]{1,2} - [0-9]{4})",
        "ranking_reporting_period_date_format": "iso-publication-week-wednesday",
        "ranking_reporting_period_max_age_days": 14,
        "ranking_expected_raw_row_count": 1,
        "ranking_evidence_url": "source_page",
        "adult_nonfiction_exclusions": ["Kinderboek"],
        "eligible_candidate_category": "adult nonfiction",
    }


def page(label="Week 40 - 2026"):
    return (
        f'<html><body><h2 class="board__title">Top Non-Fictie</h2>'
        f'<span class="board__week">{label}</span><div class="card">'
        '<div class="card__position">1</div><div class="card__title">A Book</div>'
        '<div class="card__author">Jane Author</div><div>ISBN 9780306406157</div>'
        "</div></body></html>"
    )


class Response(BytesIO):
    status = 200
    headers: ClassVar[dict[str, str]] = {}

    def __init__(self, body, url=URL):
        super().__init__(body.encode())
        self.url = url

    def geturl(self):
        return self.url


class Transport:
    def __init__(self, head=404, get=200, html=None, get_url=URL):
        self.head, self.get = head, get
        self.html, self.get_url = html or page(), get_url
        self.calls = []

    def __call__(self, request, timeout):
        method = request.get_method()
        self.calls.append(method)
        status = self.head if method == "HEAD" else self.get
        if status != 200:
            raise HTTPError(request.full_url, status, "error", {}, None)
        return Response("" if method == "HEAD" else self.html, self.get_url)


def probe(tmp_path, transport, metadata=None):
    metadata = source() if metadata is None else metadata
    return run_source_candidate_probe(
        [URL],
        contract_families=["ranking-table"],
        contract_metadata=[metadata],
        visible_title_selectors=[".board__title"],
        opener=transport,
        ledger_path=tmp_path / "ledger.json",
        feasibility_dir=tmp_path / "drafts",
        keep_captures=tmp_path / "captures",
    )


def test_explicit_exception_gets_once_and_retains_accepted_proof(tmp_path):
    transport = Transport()
    report, code = probe(tmp_path, transport)
    assert code == 0
    assert transport.calls == ["HEAD", "GET"]
    result = report["candidates"][0]
    assert result["head_status"] == 404 and result["get_status"] == 200
    assert result["raw_row_count"] == 1
    assert result["reporting_period"] == "Week 40 - 2026"
    assert list((tmp_path / "captures").glob("*.json"))


def test_default_404_refusal_remains(tmp_path):
    metadata = source()
    metadata.pop("ranking_head_404_get_fallback")
    transport = Transport()
    report, code = probe(tmp_path, transport, metadata)
    assert code == 1 and report["candidates"][0]["reason_code"] == "head_denied"
    assert transport.calls == ["HEAD"]


@pytest.mark.parametrize("head", [403, 429, 500, 502, 503])
def test_exception_never_relaxes_other_denials(tmp_path, head):
    transport = Transport(head=head)
    report, code = probe(tmp_path, transport)
    assert code == 1 and report["candidates"][0]["reason_code"] == "head_denied"
    assert transport.calls == ["HEAD"]


@pytest.mark.parametrize("get", [403, 404, 429, 500])
def test_get_must_still_succeed(tmp_path, get):
    transport = Transport(get=get)
    report, code = probe(tmp_path, transport)
    assert code == 1 and report["candidates"][0]["outcome"] == "reject"
    assert transport.calls == ["HEAD", "GET"]


@pytest.mark.parametrize(
    "html",
    [
        page().replace("Top Non-Fictie", "Fictie"),
        page().replace("Jane Author", ""),
        page().replace("9780306406157", ""),
        page().replace('class="card"', 'class="other"'),
    ],
)
def test_exception_preserves_page_and_row_gates(tmp_path, html):
    report, code = probe(tmp_path, Transport(html=html))
    assert code == 1 and report["candidates"][0]["outcome"] == "reject"


def test_exception_rejects_unrelated_get_redirect(tmp_path):
    report, code = probe(tmp_path, Transport(get_url="https://other.test/non-fictie"))
    assert code == 1 and report["candidates"][0]["reason_code"] == "unrelated_redirect"


def test_retained_head_404_evidence_requires_same_opt_in():
    evidence = {
        "source_url": URL,
        "source_page": URL,
        "resolved_url": URL,
        "html": page(),
        "head_status": 404,
        "get_status": 200,
        "reference_date": REFERENCE_DATE.isoformat(),
    }
    assert evaluate_source_feasibility(source(), evidence).accepted
    metadata = source()
    metadata.pop("ranking_head_404_get_fallback")
    assert evaluate_source_feasibility(metadata, evidence).reason_code == "head_denied"


@pytest.mark.parametrize(
    "label",
    [
        "Week 41 - 2026",
        "Week 37 - 2026",
        "Week 0 - 2026",
        "Week 54 - 2026",
        "Week 53 - 2026",
        "Week 40 - 2026 Week 39 - 2026",
    ],
)
def test_invalid_future_stale_and_duplicate_week_labels_refuse(label):
    assert ranking_page_identity(page(label), source(), REFERENCE_DATE) is None


def test_wednesday_freshness_uses_edition_week_and_rejects_before_publication():
    assert ranking_page_identity(page(), source(), date(2026, 9, 30)) is not None
    assert ranking_page_identity(page(), source(), date(2026, 9, 29)) is None
    assert ranking_page_identity(page(), source(), date(2026, 10, 14)) is not None
    assert ranking_page_identity(page(), source(), date(2026, 10, 15)) is None


def test_iso_year_boundary_week_53():
    metadata = source()
    assert ranking_page_identity(page("Week 53 - 2020"), metadata, date(2021, 1, 1)) is not None


def test_week_format_requires_visible_week_selector():
    metadata = source()
    metadata.pop("ranking_reporting_date_selector")
    assert "requires ranking_reporting_date_selector" in validate_ranking_contract_metadata(metadata)


@pytest.mark.parametrize("value", ["true", 1, None])
def test_malformed_http_opt_in_is_invalid(value):
    metadata = source()
    metadata["ranking_head_404_get_fallback"] = value
    assert "must be a boolean" in validate_ranking_contract_metadata(metadata)
