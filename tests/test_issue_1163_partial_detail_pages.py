"""Issue #1163: a bounded number of failed detail pages must not lose the source.

A ranking detail route may declare ``ranking_detail_max_failed_pages``. Up to
that many rows may lose their product page to an HTTP error, a timeout, a
dropped connection, a redirect or scope drift: those rows are skipped and
named, and the rest of the ranking is collected as incomplete coverage. One
more failure fails the source as before, and a route that declares nothing
stays strict.
"""

from __future__ import annotations

import io
import json
import socket
import ssl
from datetime import date
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.error import URLError

import pytest

import autoscout.source_collectors as collectors
from autoscout.capture_deadline import CAPTURE_DEADLINE_KEY
from autoscout.capture_deadline import CaptureDeadline
from autoscout.ranking_detail_route import RankingDetailRoute
from autoscout.ranking_detail_route import validate_ranking_detail_route
from autoscout.ranking_freshness import RankingFreshnessLedger
from autoscout.source_collection import SourceCollection
from autoscout.source_collectors import CollectorRunError
from autoscout.source_collectors import fetch_agent_source_page
from autoscout.source_collectors.agent_model_input import model_bounded_page_data
from autoscout.source_page_identity import incomplete_coverage_from_run
from autoscout.source_page_identity import incomplete_ranking_debug_for_page
from autoscout.source_page_identity import validate_ranking_detail_pages

SOURCE_URL = "https://cuspide.com/100-mas-vendidos/"
ROW_COUNT = 6
DETAIL_HTML = (
    '<div class="product_meta"><span class="sku">9780306406157</span>'
    '<span class="posted_in">Categoría: Historia de América</span></div>'
)


def _source(**overrides: Any) -> dict[str, Any]:
    source: dict[str, Any] = {
        "name": "Cúspide partial detail",
        "family": "bestseller/ranking",
        "url": SOURCE_URL,
        "active": True,
        "access_status": "open",
        "check_frequency": "weekly",
        "language": "es",
        "contract_family": "ranking-table",
        "metadata_ranking_table": True,
        "ranking_row_selector": ".category-page-row > .col",
        "ranking_rank_source": "selector",
        "ranking_rank_selector": ".livriz-ranking",
        "ranking_title_selector": ".name",
        "ranking_author_selector": ".author-product-loop",
        "ranking_author_optional": True,
        "ranking_category_marker": "Los 100 más vendidos",
        "ranking_reporting_period_undated": True,
        "ranking_expected_raw_row_count": ROW_COUNT,
        "ranking_evidence_url": "detail_page",
        "expected_signal_strength": "top ranking",
        "eligible_candidate_category": "adult nonfiction",
        "ranking_detail_link_selector": ".name",
        "ranking_detail_url_pattern": r"https://cuspide\.com/producto/[^?#]+/?",
        "ranking_detail_scope_selector": ".product_meta",
        "ranking_detail_max_pages": ROW_COUNT,
        "ranking_detail_max_failed_pages": 2,
        "ranking_detail_fields": ["isbn", "category"],
        "ranking_detail_identifier_selector": ".sku",
        "ranking_detail_identifier_pattern": "(?P<identifier>97[89][0-9]{10})",
        "ranking_detail_category_selector": ".posted_in",
        "ranking_detail_category_pattern": "Categoría:\\s*(?P<category>[^<]+)",
        "ranking_detail_category_allowlist": ["Historia de América"],
    }
    source.update(overrides)
    return source


def _strict_source(**overrides: Any) -> dict[str, Any]:
    source = _source(**overrides)
    source.pop("ranking_detail_max_failed_pages")
    return source


def _detail_url(index: int) -> str:
    return f"https://cuspide.com/producto/book-{index}/"


def _ranking_html(row_count: int = ROW_COUNT) -> str:
    rows = "".join(
        '<div class="category-page-row"><div class="col">'
        f'<p class="livriz-ranking">#{index} ranking en venta</p>'
        f'<h3 class="name"><a href="{_detail_url(index)}">Book {index}</a></h3>'
        f'<p class="author-product-loop">Author {index}</p>'
        "</div></div>"
        for index in range(1, row_count + 1)
    )
    return "<h1>Los 100 más vendidos de la semana</h1>" + rows


class _Response:
    def __init__(self, url: str, body: str, headers: dict[str, str] | None = None) -> None:
        self._url = url
        self._body = body.encode("utf-8")
        self.headers = headers or {}

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._url

    def getheader(self, _name: str, _default: str = "") -> str:
        return ""


def _failure(kind: str, url: str) -> _Response:
    if kind == "http_500":
        raise HTTPError(url, 500, "Server Error", {}, None)  # type: ignore[arg-type]
    if kind == "http_404":
        raise HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]
    if kind == "timeout":
        raise URLError(TimeoutError("timed out"))
    if kind == "connection_reset":
        raise ConnectionResetError("reset by peer")
    if kind == "redirect":
        return _Response("https://cuspide.com/producto/another-book/", DETAIL_HTML)
    if kind == "scope_missing":
        return _Response(url, "<div>no product meta</div>")
    if kind == "http_429":
        raise HTTPError(url, 429, "Too Many Requests", {}, None)  # type: ignore[arg-type]
    if kind == "http_403":
        raise HTTPError(url, 403, "Forbidden", {}, None)  # type: ignore[arg-type]
    if kind == "challenge_503":
        raise HTTPError(url, 503, "Service Unavailable", {"cf-mitigated": "challenge"}, None)  # type: ignore[arg-type]
    if kind == "captcha_200":
        return _Response(url, '<div class="g-recaptcha"></div>')
    if kind == "challenge_body_503":
        # A challenge page with no cf-mitigated header: its body names it.
        body = io.BytesIO(b'<html><div class="h-captcha"></div></html>')
        raise HTTPError(url, 503, "Service Unavailable", {}, body)  # type: ignore[arg-type]
    if kind == "challenge_header_200":
        # A 200 challenge page whose body carries no recognised marker.
        return _Response(url, "<html><p>Just a moment...</p></html>", headers={"cf-mitigated": "challenge"})
    if kind == "tls_verification":
        raise URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))
    if kind == "name_lookup":
        raise URLError(socket.gaierror(-2, "Name or service not known"))
    if kind == "raw_ssl_error":
        raise ssl.SSLError(1, "unexpected TLS failure")
    raise AssertionError(kind)


def _install_site(monkeypatch: pytest.MonkeyPatch, failing: dict[int, str], row_count: int = ROW_COUNT) -> list[str]:
    calls: list[str] = []

    def opener(request: Any, timeout: float) -> _Response:
        url = request.full_url
        calls.append(url)
        if url == SOURCE_URL:
            return _Response(url, _ranking_html(row_count))
        index = int(url.rstrip("/").rsplit("-", 1)[1])
        if index in failing:
            return _failure(failing[index], url)
        return _Response(url, DETAIL_HTML)

    monkeypatch.setattr(collectors, "urlopen", opener)
    return calls


def _fetch(source: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    return fetch_agent_source_page(Path("."), Path("."), source, reference_date=date(2026, 8, 24))


def _page(skipped: list[dict[str, str]], pages: list[int]) -> dict[str, Any]:
    return {
        "source_page": SOURCE_URL,
        "resolved_url": SOURCE_URL,
        "html": _ranking_html(),
        "detail_pages": [
            {
                "source_page": _detail_url(i),
                "detail_url": _detail_url(i),
                "resolved_url": _detail_url(i),
                "html": DETAIL_HTML,
            }
            for i in pages
        ],
        "detail_pages_skipped": skipped,
    }


def _skip(index: int, **overrides: str) -> dict[str, str]:
    entry = {"rank": str(index), "title": f"Book {index}", "detail_url": _detail_url(index), "reason": "HTTP 500"}
    entry.update(overrides)
    return entry


# --- Live fetch ---------------------------------------------------------------


@pytest.mark.parametrize("kind", ["http_500", "http_404", "timeout", "connection_reset", "redirect", "scope_missing"])
def test_failures_within_the_bound_skip_and_name_their_rows(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    calls = _install_site(monkeypatch, {2: kind, 5: kind})
    source = _source()

    page, debug = _fetch(source)

    # Every row was requested once; the failures cost no extra request.
    assert calls == [SOURCE_URL, *(_detail_url(index) for index in range(1, ROW_COUNT + 1))]
    assert [item["detail_url"] for item in page["detail_pages"]] == [_detail_url(i) for i in (1, 3, 4, 6)]
    skipped = page["detail_pages_skipped"]
    assert [(item["rank"], item["title"], item["detail_url"]) for item in skipped] == [
        ("2", "Book 2", _detail_url(2)),
        ("5", "Book 5", _detail_url(5)),
    ]
    assert all(item["reason"].strip() for item in skipped)
    assert validate_ranking_detail_pages(source, page) == ""
    # The source's diagnostics name the skipped rows as partial coverage.
    assert debug["incomplete_ranking"] is True
    assert debug["detail_pages_skipped_count"] == 2
    assert [item["title"] for item in debug["detail_pages_skipped"]] == ["Book 2", "Book 5"]
    assert debug["left_out_titles"] == ["Book 2", "Book 5"]
    assert debug["raw_row_count"] == ROW_COUNT - 2
    assert debug["expected_raw_row_count"] == ROW_COUNT
    assert "2 product pages could not be read" in debug["incomplete_ranking_warning"]


def test_one_failure_above_the_bound_fails_the_source_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_site(monkeypatch, {1: "http_500", 3: "http_500", 4: "http_404"})

    with pytest.raises(CollectorRunError, match="HTTP 404") as raised:
        _fetch(_source())

    assert raised.value.status == "failed"
    assert raised.value.debug_metadata["detail_pages_skipped_count"] == 2
    # The source stops at the failure over the bound; no later page is requested.
    assert calls[-1] == _detail_url(4)


def test_a_route_without_the_declaration_stays_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_site(monkeypatch, {2: "http_500"})

    with pytest.raises(CollectorRunError, match="HTTP 500") as raised:
        _fetch(_strict_source())

    assert "detail_pages_skipped_count" not in raised.value.debug_metadata
    assert calls == [SOURCE_URL, _detail_url(1), _detail_url(2)]


def test_a_strict_route_lets_a_dropped_connection_propagate_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_site(monkeypatch, {2: "connection_reset"})

    with pytest.raises(ConnectionResetError):
        _fetch(_strict_source())


def test_a_strict_route_keeps_its_redirect_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_site(monkeypatch, {1: "redirect"})

    with pytest.raises(CollectorRunError, match="resolved to a different linked URL") as raised:
        _fetch(_strict_source())

    assert raised.value.debug_metadata["safe_failure_status"] == "detail_redirect"
    assert "detail_failure_kind" not in raised.value.debug_metadata


@pytest.mark.parametrize("kind", ["http_429", "http_403"])
def test_a_block_or_rate_limit_is_never_skipped(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    calls = _install_site(monkeypatch, {2: kind})

    with pytest.raises(CollectorRunError) as raised:
        _fetch(_source())

    assert raised.value.status == "skipped"
    assert calls[-1] == _detail_url(2)


def test_a_bot_challenge_is_never_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_site(monkeypatch, {2: "challenge_503"})

    with pytest.raises(CollectorRunError, match="HTTP 503") as raised:
        _fetch(_source())

    assert raised.value.debug_metadata["bot_challenge"] is True
    assert calls[-1] == _detail_url(2)


def test_a_challenge_body_on_an_error_status_is_never_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_site(monkeypatch, {2: "challenge_body_503"})

    with pytest.raises(CollectorRunError, match="HTTP 503") as raised:
        _fetch(_source())

    assert raised.value.debug_metadata["bot_challenge"] is True
    assert "detail_failure_kind" not in raised.value.debug_metadata
    assert calls[-1] == _detail_url(2)


def test_a_challenge_header_on_a_200_is_never_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_site(monkeypatch, {2: "challenge_header_200"})

    with pytest.raises(CollectorRunError, match="bot-protection challenge") as raised:
        _fetch(_source())

    assert raised.value.status == "skipped"
    assert raised.value.debug_metadata["bot_challenge"] is True
    assert calls[-1] == _detail_url(2)


@pytest.mark.parametrize("kind", ["tls_verification", "name_lookup"])
def test_a_transport_failure_off_the_list_is_never_skipped(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    calls = _install_site(monkeypatch, {2: kind})

    with pytest.raises(CollectorRunError) as raised:
        _fetch(_source())

    assert raised.value.status == "failed"
    assert "detail_failure_kind" not in raised.value.debug_metadata
    assert calls[-1] == _detail_url(2)


def test_an_unexpected_error_fails_the_source_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_site(monkeypatch, {2: "raw_ssl_error"})

    with pytest.raises(ssl.SSLError):
        _fetch(_source())

    assert calls[-1] == _detail_url(2)


def test_a_captcha_page_is_never_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_site(monkeypatch, {2: "captcha_200"})

    with pytest.raises(CollectorRunError) as raised:
        _fetch(_source())

    assert raised.value.status == "skipped"
    assert calls[-1] == _detail_url(2)


def test_an_exhausted_capture_allowance_is_never_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_site(monkeypatch, {})
    clock = {"now": 0.0}
    deadline = CaptureDeadline.starting_at(0.0, 10.0, lambda: clock["now"])
    real_opener = collectors.urlopen

    def opener(request: Any, timeout: float) -> _Response:
        response = real_opener(request, timeout)
        if request.full_url == _detail_url(1):
            clock["now"] = 11.0
        return response

    monkeypatch.setattr(collectors, "urlopen", opener)

    with pytest.raises(CollectorRunError) as raised:
        _fetch(_source(**{CAPTURE_DEADLINE_KEY: deadline}))

    assert raised.value.debug_metadata["safe_failure_status"] == "capture_deadline"
    assert calls == [SOURCE_URL, _detail_url(1)]


# --- Evidence validation ------------------------------------------------------


def test_validation_accepts_the_declared_partial_result() -> None:
    page = _page([_skip(2), _skip(5)], [1, 3, 4, 6])
    assert validate_ranking_detail_pages(_source(), page) == ""


def test_validation_of_complete_evidence_is_unchanged() -> None:
    assert validate_ranking_detail_pages(_strict_source(), _page([], list(range(1, ROW_COUNT + 1)))) == ""
    complete = _page([], list(range(1, ROW_COUNT + 1)))
    del complete["detail_pages_skipped"]
    assert validate_ranking_detail_pages(_strict_source(), complete) == ""


@pytest.mark.parametrize(
    "source, skipped, pages, expected",
    [
        (_strict_source(), [_skip(2)], [1, 3, 4, 5, 6], "ranking_detail_max_failed_pages is 0"),
        (
            _source(),
            [_skip(1), _skip(2), _skip(3)],
            [4, 5, 6],
            "skips 3 pages but ranking_detail_max_failed_pages is 2",
        ),
        (
            _source(),
            [_skip(2, detail_url="https://cuspide.com/producto/book-99/")],
            [1, 3, 4, 5, 6],
            "does not match a surviving",
        ),
        (_source(), [_skip(2), _skip(2)], [1, 3, 4, 5, 6], "duplicate detail URL"),
        (_source(), [_skip(2, title="Another title")], [1, 3, 4, 5, 6], "does not match its list row"),
        (_source(), [_skip(2, rank="3")], [1, 3, 4, 5, 6], "does not match its list row"),
        (_source(), [_skip(2, reason=" ")], [1, 3, 4, 5, 6], "must state a bounded reason"),
        (_source(), [_skip(2, reason="x" * 301)], [1, 3, 4, 5, 6], "must state a bounded reason"),
        (_source(), [_skip(2)], [1, 2, 3, 4, 5, 6], "does not match surviving ranking rows"),
        (_source(), [_skip(2)], [1, 3, 4, 5], "does not match surviving ranking rows"),
        (_source(), [_skip(2)], [1, 4, 3, 5, 6], "does not match its list row"),
        (
            _source(),
            [_skip(2, detail_url="https://[cuspide.com/producto/book-2/")],
            [1, 3, 4, 5, 6],
            "does not match a surviving",
        ),
        (_source(), ["Book 2"], [1, 3, 4, 5, 6], "must be an object"),
        (_source(), {"rank": "2"}, [1, 3, 4, 5, 6], "must be a list"),
    ],
)
def test_validation_refuses_anything_looser_than_the_declared_partial_result(
    source: dict[str, Any], skipped: Any, pages: list[int], expected: str
) -> None:
    assert expected in validate_ranking_detail_pages(source, _page(skipped, pages))


def test_validation_refuses_a_skip_of_every_surviving_row() -> None:
    source = _source(ranking_expected_raw_row_count=2, ranking_detail_max_pages=3)
    page = _page([_skip(1), _skip(2)], [])
    page["html"] = _ranking_html(2)
    assert "skips every surviving ranking row" in validate_ranking_detail_pages(source, page)


def test_validation_refuses_skips_without_a_detail_route() -> None:
    source = {
        key: value
        for key, value in _source().items()
        if not key.startswith("ranking_detail_") and key != "eligible_candidate_category"
    }
    page = {"source_page": SOURCE_URL, "html": _ranking_html(), "detail_pages_skipped": [_skip(1)]}
    assert "without a declared detail route" in validate_ranking_detail_pages(source, page)


# --- Declaration --------------------------------------------------------------


def test_route_parses_the_declared_bound_and_defaults_to_strict() -> None:
    assert RankingDetailRoute.from_source(_source()).max_failed_pages == 2  # type: ignore[union-attr]
    assert RankingDetailRoute.from_source(_strict_source()).max_failed_pages == 0  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "value, expected",
    [
        (-1, "must be a non-negative integer"),
        (True, "must be a non-negative integer"),
        ("2", "must be a non-negative integer"),
        (ROW_COUNT, "must be less than ranking_detail_max_pages"),
    ],
)
def test_route_refuses_a_malformed_or_unbounded_declaration(value: object, expected: str) -> None:
    assert expected in validate_ranking_detail_route(_source(ranking_detail_max_failed_pages=value))
    # A malformed or unbounded value never loosens the parsed route, so a path
    # that reads the declaration without validating it stays strict.
    route = RankingDetailRoute.from_source(_source(ranking_detail_max_failed_pages=value))
    assert route is not None and route.max_failed_pages == 0


def test_an_unvalidated_bound_at_the_page_cap_still_fails_the_source(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_site(monkeypatch, {2: "http_500"})

    with pytest.raises(CollectorRunError, match="HTTP 500"):
        _fetch(_source(ranking_detail_max_failed_pages=ROW_COUNT))


def test_route_accepts_zero_and_a_bound_below_the_page_cap() -> None:
    assert validate_ranking_detail_route(_source(ranking_detail_max_failed_pages=0)) == ""
    assert validate_ranking_detail_route(_source(ranking_detail_max_failed_pages=ROW_COUNT - 1)) == ""


def test_configured_cuspide_declares_a_bounded_tolerance() -> None:
    from autoscout.config import load_validated_config

    root = Path(__file__).resolve().parents[1]
    for config in ("current-weekly-portfolio.json", "current-weekly-live.json", "current-weekly-pilot-live.json"):
        sources = load_validated_config(root / "configs" / config)["sources"]
        (cuspide,) = [source for source in sources if source.get("name") == "Cúspide 100 más vendidos"]
        route = RankingDetailRoute.from_source(cuspide)
        assert route is not None
        assert 0 < route.max_failed_pages < route.max_pages


# --- Model input and coverage -------------------------------------------------


def test_the_model_never_sees_a_skipped_row() -> None:
    source = _source()
    page = _page([_skip(2), _skip(5)], [1, 3, 4, 6])
    bounded, _debug = model_bounded_page_data(page, 120_000, source=source)
    assert "Book 2" not in bounded["html"]
    assert "Book 5" not in bounded["html"]
    assert all(f"Book {index}" in bounded["html"] for index in (1, 3, 4, 6))
    assert len(bounded["detail_pages"]) == 4


def test_model_input_survives_a_malformed_skip_url() -> None:
    page = _page([_skip(2, detail_url="https://[broken")], [1, 3, 4, 5, 6])
    bounded, _debug = model_bounded_page_data(page, 120_000, source=_source())
    assert "Book 2" in bounded["html"]


def test_coverage_names_skips_beside_a_short_list() -> None:
    source = _source(ranking_expected_raw_row_count=8, ranking_accept_incomplete=True, ranking_detail_max_pages=8)
    debug = incomplete_ranking_debug_for_page(source, _page([_skip(2)], [1, 3, 4, 5, 6]))
    assert debug["expected_raw_row_count"] == 8
    assert debug["ranking_list_row_count"] == ROW_COUNT
    assert debug["raw_row_count"] == ROW_COUNT - 1
    assert "exposes 6 of 8 expected raw rows" in debug["incomplete_ranking_warning"]
    assert "1 product page could not be read" in debug["incomplete_ranking_warning"]
    assert debug["left_out_titles"] == ["Book 2"]


def test_complete_detail_evidence_reports_full_coverage() -> None:
    page = _page([], list(range(1, ROW_COUNT + 1)))
    assert incomplete_ranking_debug_for_page(_source(), page) == {}


# --- End to end ---------------------------------------------------------------


def _mention(index: int) -> dict[str, Any]:
    return {
        "title": f"Book {index}",
        "primary_authors": [f"Author {index}"],
        "isbn": "9780306406157",
        "current_rank": str(index),
        "source_week": "2026-08-24",
        "source_url": SOURCE_URL,
        "evidence_url": _detail_url(index),
        "signal_type": "bestseller/ranking",
        "signal_strength": "top ranking",
        "candidate_category": "adult nonfiction",
        "evidence_note": f"Rank #{index} in the Cúspide ranking.",
        "english_title": f"Book {index}",
        "search_translation": f"Книга {index}",
        "author_transliteration_note": f"Автор {index}",
    }


def test_live_collection_succeeds_names_the_skipped_rows_and_replays(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_site(monkeypatch, {2: "http_500", 5: "timeout"})
    source = _source(
        extraction_method="agent-extraction-live",
        enable_live=True,
        extraction_backend="fake",
        # The extractor also returns a skipped row: with no captured product
        # page it cannot be grounded, so it is dropped, never collected.
        fake_extraction_response={"mentions": [_mention(1), _mention(2)]},
        capture_live_response_file="cuspide-page-capture.json",
    )
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    output_dir = tmp_path / "run"

    result = SourceCollection(config_path, output_dir, "2026-08-24", RankingFreshnessLedger()).collect([source])

    assert result.failed_source_count == 0
    run = result.source_runs[0]
    assert run["status"] == "collected"
    assert "2 product pages could not be read" in run["reason"]
    assert [mention["title"] for mention in result.source_mentions] == ["Book 1"]
    coverage = incomplete_coverage_from_run(run)
    assert coverage is not None
    assert coverage["left_out_titles"] == ["Book 2", "Book 5"]
    assert (coverage["observed_rows"], coverage["expected_rows"]) == (ROW_COUNT - 2, ROW_COUNT)

    # The written capture keeps the skipped rows, so its replay validates the
    # same partial evidence without network access.
    (capture_path,) = list(output_dir.rglob("cuspide-page-capture.json"))
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    assert [item["title"] for item in capture["detail_pages_skipped"]] == ["Book 2", "Book 5"]
    assert validate_ranking_detail_pages(source, capture) == ""
    capture.pop("detail_pages_skipped")
    assert "does not match surviving ranking rows" in validate_ranking_detail_pages(source, capture)
