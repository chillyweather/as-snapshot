"""Offline tests for issue #717, capture bounds that bound wall-clock time.

The capture stage declared two bounds that nothing enforced.  The stage budget
was the flat base constant because both production links defaulted to it, so
the issue #595 derivation never ran in production; and the per-source value was
handed to the HTTP layer as a per-socket-operation timeout, so a source with a
declared detail route could issue one request per row and spend
``(1 + max_pages) x timeout`` seconds against a bound of ``timeout``.

Both bounds now follow the same declaration.  The stage budget grows by each
declared detail page, and so does the allowance of the source that declared it:
a source's clamped socket timeout plus the measured per-page allowance for
every page its route declares, never more than the stage budget still left.
That allowance is a wall-clock deadline across the source's whole capture.

These tests observe the production capture adapter, one level above the
worklist-level budget tests of issue #595, because the defect is in which
budget the production chain chooses and only this seam can see that choice.
The clock, the opener and the response bodies are injected: no test here
waits, and no test here makes a network request.

Issue #1005 changed one product rule these tests were written under.  A source
that exhausts its per-source capture bound by a timeout now gets exactly one
retry with the larger retry socket ceiling while the capture stage budget has
room.  The #717 invariants are unchanged and are asserted per attempt: each
attempt stops at its own deadline, every request's socket timeout is capped by
the time its attempt has left and never widened past the timeout its request
used (the retry's ceiling is the retry socket timeout), a body that keeps
arriving or arrives after a deadline is never published, one slow source does
not consume the sources after it, the whole stage finishes inside the budget it
reports including retries, and the heartbeat reports the bound actually
enforced.  The first-attempt assertions of each test are kept as they were;
the retry is slowed (RETRY_REQUEST_SECONDS, RETRY_SLICE_BYTES) so that it also
stops at its own deadline, which keeps each test's "ends failed at its deadline"
point.  Product-rule change authorized by Dima for issue #1005.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest

import autoscout.source_collectors as collectors
from autoscout.codex_recovery import ProductionCodexCaptureAdapter
from autoscout.codex_workflow import prepare_codex_extraction
from autoscout.config import resolve_weekly_portfolio
from autoscout.ranking_detail_route import RankingDetailRoute
from autoscout.runtime_budget import CAPTURE_DETAIL_PAGE_ALLOWANCE_SECONDS
from autoscout.runtime_budget import CAPTURE_RUNTIME_BUDGET_SECONDS
from autoscout.runtime_budget import CAPTURE_SOURCE_RETRY_TIMEOUT_SECONDS
from autoscout.runtime_budget import CAPTURE_SOURCE_TIMEOUT_SECONDS
from autoscout.runtime_budget import capture_runtime_budget_for
from autoscout.runtime_budget import capture_source_allowance_for
from autoscout.runtime_budget import declared_extra_request_count
from autoscout.source_collection import codex_capture_filename

RUN_DATE = "2026-08-24"
REPORTING_PERIOD = "2026-08-17 - 2026-08-24"
# The per-source socket ceiling these tests hand the adapter, and what one fake
# request spends: a page that takes its whole socket budget across receives
# that each stay inside it.  Every value here is exact in binary floating
# point, so the arithmetic below is exact rather than approximate.
SOCKET_TIMEOUT_SECONDS = 4.0
REQUEST_SECONDS = 4.0
# A page that stays inside the measured per-detail-page allowance instead.
FAST_REQUEST_SECONDS = 2.0
DETAIL_MAX_PAGES = 11
DETAIL_ROWS = 11
# What a declared detail route earns: the socket ceiling plus the measured
# per-detail-page allowance for every page the route declares.
DETAIL_ALLOWANCE_SECONDS = SOCKET_TIMEOUT_SECONDS + DETAIL_MAX_PAGES * CAPTURE_DETAIL_PAGE_ALLOWANCE_SECONDS
# How many requests at REQUEST_SECONDS fit before that allowance is gone.
REQUESTS_BEFORE_DEADLINE = math.ceil(DETAIL_ALLOWANCE_SECONDS / REQUEST_SECONDS)
# One slice of a body that keeps arriving: a single receive, well inside the
# socket timeout, of which the allowance holds exactly this many.
DRIP_SLICE_SECONDS = 1.0
DRIP_SLICE_BYTES = 32
SLICES_BEFORE_DEADLINE = int(DETAIL_ALLOWANCE_SECONDS / DRIP_SLICE_SECONDS)
# Issue #1005: a timed-out source is retried once with this socket ceiling, and
# its declared detail route earns the same per-page allowance on top of it.
RETRY_SOCKET_SECONDS = CAPTURE_SOURCE_RETRY_TIMEOUT_SECONDS
RETRY_DETAIL_ALLOWANCE_SECONDS = RETRY_SOCKET_SECONDS + DETAIL_MAX_PAGES * CAPTURE_DETAIL_PAGE_ALLOWANCE_SECONDS
# The retry is made slow enough that it also stops at its own deadline, so every
# test that ends failed at its deadline still ends failed, now after two attempts.
RETRY_REQUEST_SECONDS = 6.0
RETRY_REQUESTS_BEFORE_DEADLINE = math.ceil(RETRY_DETAIL_ALLOWANCE_SECONDS / RETRY_REQUEST_SECONDS)
# Half-size slices on the retry keep the 11-row list body longer than what the
# retry's allowance can read, so the body is still cut at the deadline.
RETRY_SLICE_BYTES = DRIP_SLICE_BYTES // 2
RETRY_SLICES_BEFORE_DEADLINE = int(RETRY_DETAIL_ALLOWANCE_SECONDS / DRIP_SLICE_SECONDS)
# The live measurement recorded in issue #595 on 2026-08-24: one list page and
# 100 product pages took 133.8s in total.
MEASURED_CUSPIDE_SECONDS = 133.8


class FakeClock:
    """A monotonic clock that only moves when a fake request is served."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeResponse:
    def __init__(self, url: str, body: str) -> None:
        self._url = url
        self._body = body.encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._url

    def getheader(self, _name: str, _default: str = "") -> str:
        return ""


def list_html(rows: int) -> str:
    row_markup = "".join(
        f'<div class="row"><span class="rank">{index}</span>'
        f'<span class="title">Book {index}</span>'
        f'<span class="author">Author {index}</span>'
        f'<a class="detail-link" href="/books/{index}">Details</a></div>'
        for index in range(1, rows + 1)
    )
    return (
        f'<h1 class="heading">Adult nonfiction {REPORTING_PERIOD}</h1>'
        f"{row_markup}"
        f"<p>Source: Circana BookScan {REPORTING_PERIOD}</p>"
    )


def detail_html(index: int) -> str:
    return (
        f'<div class="product-detail"><span class="isbn">ISBN: 978030640{6156 + index:04d}</span>'
        f'<span class="category">Category: Historia de América</span></div>'
    )


def single_page_source(name: str = "Single page source") -> dict[str, Any]:
    return {
        "name": name,
        "family": "bestseller/ranking",
        "url": "https://single.test/ranking",
        "active": True,
        "access_status": "open",
        "language": "en",
        "contract_family": "ranking-table",
        "metadata_ranking_table": True,
        "ranking_evidence_url": "source_page",
        "ranking_row_selector": ".row",
        "ranking_rank_source": "selector",
        "ranking_rank_selector": ".rank",
        "ranking_title_selector": ".title",
        "ranking_author_selector": ".author",
        "ranking_category_marker": "Adult nonfiction",
        "ranking_reporting_period_pattern": rf"(?P<period>{REPORTING_PERIOD})",
        "ranking_reporting_period_date_format": "%Y-%m-%d",
        "ranking_reporting_period_max_age_days": 14,
        "ranking_identifier_pattern": r"(?P<identifier>97[89][0-9-]{10,20})",
        "ranking_expected_raw_row_count": 1,
        "adult_nonfiction_exclusions": ["activity book"],
        "expected_page_heading": f"Adult nonfiction {REPORTING_PERIOD}",
        "visible_title_selector": ".heading",
    }


def detail_route_source(
    *,
    name: str = "Detail route source",
    url: str = "https://example.test/ranking",
    rows: int = DETAIL_ROWS,
    max_pages: int = DETAIL_MAX_PAGES,
) -> dict[str, Any]:
    source = single_page_source(name)
    source.update(
        {
            "url": url,
            "ranking_evidence_url": "detail_page",
            "ranking_expected_raw_row_count": rows,
            "ranking_detail_link_selector": ".detail-link",
            "ranking_detail_url_pattern": r"https://[a-z.]+/books/[0-9]+",
            "ranking_detail_scope_selector": ".product-detail",
            "ranking_detail_fields": ["isbn", "category"],
            "ranking_detail_identifier_selector": ".isbn",
            "ranking_detail_identifier_pattern": r"ISBN:\s*(?P<identifier>97[89][0-9-]{10,20})",
            "ranking_detail_category_selector": ".category",
            "ranking_detail_category_pattern": r"Category:\s*(?P<category>[^<]+)",
            "ranking_detail_category_allowlist": ["Historia de América"],
            "ranking_detail_max_pages": max_pages,
            "eligible_candidate_category": "adult nonfiction",
            "ranking_identifier_selector": "",
            "ranking_identifier_attribute": "",
            "ranking_identifier_pattern": "",
        }
    )
    return source


def paged_source(*, name: str = "Paged source", max_pages: int = 4, identity: bool = False) -> dict[str, Any]:
    """A source that declares its pages the way the live Boklista source does."""
    source = single_page_source(name)
    source.update(
        {
            "ranking_request_method": "POST",
            "ranking_request_body": ["action=load_book_archive_list", "paged=1"],
            "ranking_request_page_field": "paged",
            "ranking_request_max_pages": max_pages,
            "ranking_response_html_field": "html",
            "ranking_response_last_page_field": "last",
        }
    )
    if identity:
        source.update(
            {
                "ranking_identity_url_template": "https://single.test/book-list/{year}-{week}/",
                "ranking_identity_fields": ["year=sel_year", "week=sel_week"],
            }
        )
    return source


def amazon_charts_source(name: str = "Amazon charts source") -> dict[str, Any]:
    """A source that reads the landing page before the dated chart."""
    source = single_page_source(name)
    source["amazon_charts_weekly"] = True
    return source


def write_config(tmp_path: Path, sources: list[dict[str, Any]]) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"run_date": RUN_DATE, "sources": sources}, ensure_ascii=False), encoding="utf-8")
    return path


def expected_timeouts(socket_cap: float, allowance: float, cost: float) -> list[float]:
    """The socket timeout of each request of one attempt, given its deadline.

    A request keeps the socket ceiling until the attempt has less than that
    left, and the last one is cut to the time remaining.  The request count is
    how many costly requests fit in the allowance.
    """
    return [min(socket_cap, allowance - index * cost) for index in range(math.ceil(allowance / cost))]


def install_opener(
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    sources: list[dict[str, Any]],
    *,
    seconds_per_request: float = REQUEST_SECONDS,
    seconds_by_host: Mapping[str, float] | None = None,
    retry_seconds_per_request: float = RETRY_REQUEST_SECONDS,
) -> tuple[list[str], list[float]]:
    """Serve every ranking and detail page from memory, on the fake clock.

    Each list page carries exactly the row count its own source declares, so
    the page these tests serve is one a real capture would accept.  A host may
    be given its own per-request cost, which is how one portfolio can hold both
    a slow source and a source that answers promptly.  A source's attempts are
    told apart by how many times its list page has been fetched: the second
    fetch is the issue #1005 retry, whose requests cost ``retry_seconds_per_request``.
    """
    rows_by_url = {str(source["url"]): int(source["ranking_expected_raw_row_count"]) for source in sources}
    calls: list[str] = []
    timeouts: list[float] = []
    list_fetches: dict[str, int] = {}

    def opener(request: Any, timeout: float) -> FakeResponse:
        url = request.full_url
        calls.append(url)
        timeouts.append(timeout)
        netloc = urlsplit(url).netloc
        tail = url.rsplit("/", 1)[-1]
        if not tail.isdigit():
            list_fetches[netloc] = list_fetches.get(netloc, 0) + 1
        is_retry = list_fetches.get(netloc, 0) >= 2
        default_cost = retry_seconds_per_request if is_retry else seconds_per_request
        cost = (seconds_by_host or {}).get(netloc, default_cost)
        if cost > timeout:
            # A socket gives up once one receive outlives the timeout it was
            # handed, and the deadline shrinks that timeout as it nears.
            clock.advance(timeout)
            raise TimeoutError("fake socket timeout")
        clock.advance(cost)
        if tail.isdigit():
            return FakeResponse(url, detail_html(int(tail)))
        return FakeResponse(url, list_html(rows_by_url[url]))

    monkeypatch.setattr(collectors, "urlopen", opener)
    return calls, timeouts


def capture(
    tmp_path: Path,
    clock: FakeClock,
    sources: list[dict[str, Any]],
    *,
    runtime_budget_seconds: float | None = None,
) -> dict[str, Any]:
    config = write_config(tmp_path, sources)
    # Built the way production builds it: no explicit budget unless the case
    # is about an explicit one, so the adapter's own default is under test.
    options: dict[str, Any] = {"per_source_timeout_seconds": SOCKET_TIMEOUT_SECONDS}
    if runtime_budget_seconds is not None:
        options["runtime_budget_seconds"] = runtime_budget_seconds
    adapter = ProductionCodexCaptureAdapter(clock, **options)
    return adapter.prepare(config, tmp_path / "run", live=True, effective_date=RUN_DATE)


def run_status(result: dict[str, Any], name: str) -> dict[str, Any]:
    return next(run for run in result["source_runs"] if run["source_name"] == name)


def snapshot_rows(tmp_path: Path) -> list[dict[str, Any]]:
    snapshot = json.loads((tmp_path / "run" / "progress-snapshot.json").read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = snapshot["rows"]
    return rows


def test_the_production_adapter_reports_the_budget_the_portfolio_declares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #595 derivation was inert: both production links defaulted to 150."""
    clock = FakeClock()
    source = detail_route_source(rows=2, max_pages=100)
    install_opener(monkeypatch, clock, [source])
    result = capture(tmp_path, clock, [source])

    expected = CAPTURE_RUNTIME_BUDGET_SECONDS + 100 * CAPTURE_DETAIL_PAGE_ALLOWANCE_SECONDS
    assert result["runtime_budget_seconds"] == expected
    assert result["runtime_budget_seconds"] != CAPTURE_RUNTIME_BUDGET_SECONDS


def test_the_workflow_capture_entry_point_also_derives_its_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other production link: prepare_codex_extraction's own default."""
    clock = FakeClock()
    source = detail_route_source(rows=2, max_pages=100)
    install_opener(monkeypatch, clock, [source])
    config = write_config(tmp_path, [source])
    output_dir = tmp_path / "run"

    assert prepare_codex_extraction(config, output_dir, live=True, effective_date=RUN_DATE, monotonic=clock) == 0

    expected = CAPTURE_RUNTIME_BUDGET_SECONDS + 100 * CAPTURE_DETAIL_PAGE_ALLOWANCE_SECONDS
    snapshot = json.loads((output_dir / "progress-snapshot.json").read_text(encoding="utf-8"))
    # What the heartbeat has left of the derived budget once the list page and
    # both detail pages have been fetched.
    spent = 3 * REQUEST_SECONDS
    assert snapshot["rows"][0]["remaining_runtime_budget_seconds"] == pytest.approx(expected - spent, abs=1.0)
    worklist = json.loads((output_dir / "codex-extraction-worklist.json").read_text(encoding="utf-8"))
    assert worklist["runtime_budget_seconds"] == expected


def test_a_portfolio_without_detail_routes_still_reports_the_base_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    source = single_page_source()
    install_opener(monkeypatch, clock, [source], seconds_per_request=FAST_REQUEST_SECONDS)
    result = capture(tmp_path, clock, [source])

    assert result["runtime_budget_seconds"] == CAPTURE_RUNTIME_BUDGET_SECONDS


def test_an_explicitly_supplied_budget_still_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    source = detail_route_source(rows=2)
    install_opener(monkeypatch, clock, [source])
    result = capture(tmp_path, clock, [source], runtime_budget_seconds=42.0)

    assert result["runtime_budget_seconds"] == 42.0


def test_a_non_positive_explicit_budget_is_still_rejected() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        ProductionCodexCaptureAdapter(runtime_budget_seconds=0)
    with pytest.raises(ValueError, match="must be positive"):
        ProductionCodexCaptureAdapter(per_source_timeout_seconds=-1)


def test_a_detail_route_source_stops_at_its_per_source_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declared page cap no longer decides how long one source may run.

    Issue #1005: each attempt stops at its own allowance.  The source ends
    failed at the retry's deadline, after both attempts are on record.
    """
    clock = FakeClock()
    source = detail_route_source()
    calls, _timeouts = install_opener(monkeypatch, clock, [source])
    result = capture(tmp_path, clock, [source])

    # One list page plus detail pages until the allowance is gone.  A page here
    # costs more than the allowance a declared page earns, so the source stops
    # short of the 1 + 11 requests its declared cap would otherwise permit.
    # The retry is held to its own, larger allowance and stops the same way.
    assert len(calls) == REQUESTS_BEFORE_DEADLINE + RETRY_REQUESTS_BEFORE_DEADLINE
    assert REQUESTS_BEFORE_DEADLINE < 1 + DETAIL_MAX_PAGES
    run = run_status(result, source["name"])
    assert run["status"] == "failed"
    first, retry = run["capture_attempts"]
    assert first["reason"] == f"capture per-source bound exhausted after {DETAIL_ALLOWANCE_SECONDS:.1f}s"
    assert first["safe_failure_status"] == "capture_deadline"
    assert first["duration_seconds"] <= DETAIL_ALLOWANCE_SECONDS
    assert retry["reason"] == f"capture per-source bound exhausted after {RETRY_DETAIL_ALLOWANCE_SECONDS:.1f}s"
    assert retry["safe_failure_status"] == "capture_deadline"
    assert retry["socket_timeout_seconds"] == RETRY_SOCKET_SECONDS
    assert retry["duration_seconds"] <= RETRY_DETAIL_ALLOWANCE_SECONDS
    assert run["reason"] == retry["reason"]
    assert run["duration_seconds"] <= DETAIL_ALLOWANCE_SECONDS + RETRY_DETAIL_ALLOWANCE_SECONDS
    # ADR 0011 requires a complete detail page for every surviving row, so a
    # source cut at its deadline publishes no capture at all.
    assert list((tmp_path / "run").glob("codex-capture-*.json")) == []


def test_a_source_inside_the_measured_per_page_allowance_captures_every_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The allowance is sized by the measurement, so a page that meets it fits."""
    clock = FakeClock()
    source = detail_route_source()
    calls, _timeouts = install_opener(
        monkeypatch, clock, [source], seconds_per_request=CAPTURE_DETAIL_PAGE_ALLOWANCE_SECONDS
    )
    result = capture(tmp_path, clock, [source])

    assert len(calls) == 1 + DETAIL_ROWS
    run = run_status(result, source["name"])
    assert run["status"] == "codex-captured"
    assert run["duration_seconds"] <= DETAIL_ALLOWANCE_SECONDS


def test_every_request_of_a_bounded_source_is_capped_by_the_time_it_has_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The socket timeout still applies; the deadline shrinks it as it nears."""
    clock = FakeClock()
    source = detail_route_source()
    _calls, timeouts = install_opener(monkeypatch, clock, [source])
    capture(tmp_path, clock, [source])

    # Every request keeps the full socket timeout until the source has less
    # than that left, and the last one is cut to exactly the time remaining.
    assert timeouts[:REQUESTS_BEFORE_DEADLINE] == [SOCKET_TIMEOUT_SECONDS] * (REQUESTS_BEFORE_DEADLINE - 1) + [
        DETAIL_ALLOWANCE_SECONDS - (REQUESTS_BEFORE_DEADLINE - 1) * REQUEST_SECONDS
    ]
    # The retry is capped the same way by its own ceiling and allowance, never
    # by the first attempt's socket timeout.
    assert timeouts[REQUESTS_BEFORE_DEADLINE:] == expected_timeouts(
        RETRY_SOCKET_SECONDS, RETRY_DETAIL_ALLOWANCE_SECONDS, RETRY_REQUEST_SECONDS
    )
    assert max(timeouts[REQUESTS_BEFORE_DEADLINE:]) == RETRY_SOCKET_SECONDS


def test_a_detail_route_source_that_finishes_in_time_keeps_every_detail_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0011 evidence is unchanged for a source inside its allowance."""
    clock = FakeClock()
    source = detail_route_source(rows=2)
    calls, _timeouts = install_opener(monkeypatch, clock, [source])
    result = capture(tmp_path, clock, [source])

    assert len(calls) == 3
    run = run_status(result, source["name"])
    assert run["status"] == "codex-captured"
    capture_file = json.loads((tmp_path / "run" / codex_capture_filename(source)).read_text(encoding="utf-8"))
    assert [page["detail_url"] for page in capture_file["detail_pages"]] == [
        "https://example.test/books/1",
        "https://example.test/books/2",
    ]


def test_a_single_page_source_is_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    source = single_page_source()
    calls, timeouts = install_opener(monkeypatch, clock, [source], seconds_per_request=FAST_REQUEST_SECONDS)
    result = capture(tmp_path, clock, [source])

    assert len(calls) == 1
    # No detail route, so the allowance is the clamped socket timeout it
    # always was, and the one request carries exactly that.
    assert timeouts == [SOCKET_TIMEOUT_SECONDS]
    assert run_status(result, source["name"])["status"] == "codex-captured"


def test_one_slow_source_no_longer_consumes_the_sources_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    slow = detail_route_source()
    following = single_page_source("Following source")
    calls, _timeouts = install_opener(
        monkeypatch,
        clock,
        [slow, following],
        seconds_by_host={urlsplit(str(following["url"])).netloc: FAST_REQUEST_SECONDS},
    )
    result = capture(tmp_path, clock, [slow, following])

    slow_run = run_status(result, slow["name"])
    assert slow_run["status"] == "failed"
    assert [attempt["attempt"] for attempt in slow_run["capture_attempts"]] == [1, 2]
    assert run_status(result, following["name"])["status"] == "codex-captured"
    # The slow source's retry comes after the following source has been
    # captured: the slow source never holds the following one back.
    slow_fetches = [index for index, url in enumerate(calls) if url == slow["url"]]
    assert calls.index(following["url"]) < slow_fetches[1]


def test_the_stage_finishes_inside_the_budget_it_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    single = single_page_source()
    sources = [
        detail_route_source(name="First detail source", url="https://first.test/ranking"),
        detail_route_source(name="Second detail source", url="https://second.test/ranking"),
        single,
    ]
    install_opener(
        monkeypatch,
        clock,
        sources,
        seconds_by_host={urlsplit(str(single["url"])).netloc: FAST_REQUEST_SECONDS},
    )
    result = capture(tmp_path, clock, sources)

    # The total clock includes both detail sources' retries.
    assert result["duration_seconds"] <= result["runtime_budget_seconds"]
    first_bounds = {
        str(source["name"]): capture_source_allowance_for(source, SOCKET_TIMEOUT_SECONDS) for source in sources
    }
    retry_bounds = {
        str(source["name"]): capture_source_allowance_for(source, RETRY_SOCKET_SECONDS) for source in sources
    }
    for run in result["source_runs"]:
        name = run["source_name"]
        if "capture_attempts" not in run:
            assert run["duration_seconds"] <= first_bounds[name]
            continue
        first, retry = run["capture_attempts"]
        assert first["duration_seconds"] <= first_bounds[name]
        assert retry["duration_seconds"] <= retry_bounds[name]
    retried = [run["source_name"] for run in result["source_runs"] if "capture_attempts" in run]
    assert retried == ["First detail source", "Second detail source"]


def test_the_heartbeat_reports_the_bound_that_is_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    source = detail_route_source()
    install_opener(monkeypatch, clock, [source])
    result = capture(tmp_path, clock, [source])

    row = next(row for row in snapshot_rows(tmp_path) if row["source_name"] == source["name"])
    # The heartbeat reports the bound that is enforced, which for a declared
    # detail route is the allowance it earns, not the per-request ceiling.  The
    # row is the retry's, the last attempt made, so it reports the retry's
    # allowance and that attempt's elapsed time.
    assert row["per_source_timeout_seconds"] == RETRY_DETAIL_ALLOWANCE_SECONDS
    assert row["elapsed_seconds"] <= row["per_source_timeout_seconds"]
    assert row["outcome"] == "failed"
    assert result["per_source_timeout_seconds"] == SOCKET_TIMEOUT_SECONDS


class DripResponse(FakeResponse):
    """A response whose body keeps arriving, one slice at a time.

    The socket timeout bounds one receive, so a server that never stops
    sending keeps a single read alive indefinitely.  ``read1`` is what the
    real ``http.client.HTTPResponse`` offers and what the bounded decoder
    reads in slices, so the deadline can be checked between them.
    """

    def __init__(self, url: str, body: str, *, clock: FakeClock, seconds_per_slice: float, slice_bytes: int) -> None:
        super().__init__(url, body)
        self._clock = clock
        self._seconds_per_slice = seconds_per_slice
        self._slice_bytes = slice_bytes
        self._offset = 0
        self.slices_served = 0

    def read1(self, limit: int = -1) -> bytes:
        self._clock.advance(self._seconds_per_slice)
        self.slices_served += 1
        size = self._slice_bytes if limit < 0 else min(self._slice_bytes, limit)
        served = self._body[self._offset : self._offset + size]
        self._offset += len(served)
        return served

    def read(self, limit: int = -1) -> bytes:
        # Taking the whole body in one call still waits for every slice the
        # server sends: a socket timeout bounds one receive, not a body, which
        # is exactly how a read outlives the source's allowance.
        remaining = self._body[self._offset :]
        slices = max(1, math.ceil(len(remaining) / self._slice_bytes))
        self._clock.advance(self._seconds_per_slice * slices)
        self.slices_served += slices
        self._offset = len(self._body)
        return remaining


class OverdueResponse(FakeResponse):
    """A whole body that only finishes arriving after the deadline passed."""

    def __init__(self, url: str, body: str, *, clock: FakeClock, seconds: float) -> None:
        super().__init__(url, body)
        self._clock = clock
        self._seconds = seconds

    def read(self, _limit: int = -1) -> bytes:
        self._clock.advance(self._seconds)
        return self._body


def install_response(monkeypatch: pytest.MonkeyPatch, response_for: Any) -> list[str]:
    """Serve one prepared response per request without charging for the call.

    ``response_for(url, attempt)`` is told which attempt the request belongs
    to: the first fetch of a URL is attempt 1, the issue #1005 retry's is 2.
    """
    calls: list[str] = []
    attempts: dict[str, int] = {}

    def opener(request: Any, timeout: float) -> Any:
        url = request.full_url
        calls.append(url)
        attempts[url] = attempts.get(url, 0) + 1
        return response_for(url, attempts[url])

    monkeypatch.setattr(collectors, "urlopen", opener)
    return calls


def test_a_body_that_keeps_arriving_is_cut_at_the_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A socket timeout bounds one receive; the deadline bounds the body."""
    clock = FakeClock()
    source = detail_route_source()
    served: list[DripResponse] = []
    body_bytes = len(list_html(DETAIL_ROWS).encode("utf-8"))

    def response_for(url: str, attempt: int) -> DripResponse:
        tail = url.rsplit("/", 1)[-1]
        body = detail_html(int(tail)) if tail.isdigit() else list_html(DETAIL_ROWS)
        response = DripResponse(
            url,
            body,
            clock=clock,
            seconds_per_slice=DRIP_SLICE_SECONDS,
            slice_bytes=DRIP_SLICE_BYTES if attempt == 1 else RETRY_SLICE_BYTES,
        )
        served.append(response)
        return response

    calls = install_response(monkeypatch, response_for)
    result = capture(tmp_path, clock, [source])

    assert calls == [source["url"], source["url"]]
    # The read stopped when the allowance ran out rather than when the body
    # ended: the list page still had slices left to send, in each attempt.
    assert served[0].slices_served == SLICES_BEFORE_DEADLINE
    assert served[0].slices_served * DRIP_SLICE_BYTES < body_bytes
    assert served[1].slices_served == RETRY_SLICES_BEFORE_DEADLINE
    assert served[1].slices_served * RETRY_SLICE_BYTES < body_bytes
    run = run_status(result, source["name"])
    assert run["status"] == "failed"
    first, retry = run["capture_attempts"]
    assert first["reason"] == f"capture per-source bound exhausted after {DETAIL_ALLOWANCE_SECONDS:.1f}s"
    assert retry["reason"] == f"capture per-source bound exhausted after {RETRY_DETAIL_ALLOWANCE_SECONDS:.1f}s"
    assert run["reason"] == retry["reason"]
    assert first["duration_seconds"] <= DETAIL_ALLOWANCE_SECONDS
    assert retry["duration_seconds"] <= RETRY_DETAIL_ALLOWANCE_SECONDS
    assert list((tmp_path / "run").glob("codex-capture-*.json")) == []


def test_a_body_that_arrives_after_the_deadline_is_never_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An overdue page is refused rather than captured as current evidence."""
    clock = FakeClock()
    source = single_page_source()
    overdue_seconds = 2.25 * SOCKET_TIMEOUT_SECONDS
    # The retry's body is overdue against its own, larger bound too.
    retry_overdue_seconds = 2.25 * RETRY_SOCKET_SECONDS
    calls = install_response(
        monkeypatch,
        lambda url, attempt: OverdueResponse(
            url,
            list_html(1),
            clock=clock,
            seconds=overdue_seconds if attempt == 1 else retry_overdue_seconds,
        ),
    )
    result = capture(tmp_path, clock, [source])

    assert calls == [source["url"], source["url"]]
    run = run_status(result, source["name"])
    assert run["status"] == "failed"
    first, retry = run["capture_attempts"]
    assert first["reason"] == f"capture per-source bound exhausted after {SOCKET_TIMEOUT_SECONDS:.1f}s"
    assert retry["reason"] == f"capture per-source bound exhausted after {RETRY_SOCKET_SECONDS:.1f}s"
    assert run["reason"] == retry["reason"]
    assert list((tmp_path / "run").glob("codex-capture-*.json")) == []


def test_the_allowance_follows_the_declared_detail_route() -> None:
    """The same declaration that grows the stage budget grows the allowance."""
    assert capture_source_allowance_for(single_page_source(), CAPTURE_SOURCE_TIMEOUT_SECONDS) == (
        CAPTURE_SOURCE_TIMEOUT_SECONDS
    )
    assert capture_source_allowance_for(detail_route_source(rows=2, max_pages=100), CAPTURE_SOURCE_TIMEOUT_SECONDS) == (
        CAPTURE_SOURCE_TIMEOUT_SECONDS + 100 * CAPTURE_DETAIL_PAGE_ALLOWANCE_SECONDS
    )
    small = capture_source_allowance_for(detail_route_source(rows=2, max_pages=10), CAPTURE_SOURCE_TIMEOUT_SECONDS)
    large = capture_source_allowance_for(detail_route_source(rows=2, max_pages=100), CAPTURE_SOURCE_TIMEOUT_SECONDS)
    assert large - small == 90 * CAPTURE_DETAIL_PAGE_ALLOWANCE_SECONDS


def test_the_allowance_derivation_reads_no_clock_and_makes_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the allowance derivation must be a pure function of configuration")

    monkeypatch.setattr("time.monotonic", forbidden)
    monkeypatch.setattr("time.time", forbidden)
    assert capture_source_allowance_for(detail_route_source(), CAPTURE_SOURCE_TIMEOUT_SECONDS) > 0


def test_the_configured_detail_route_now_has_room_for_its_measured_capture() -> None:
    """The portfolio's own detail route: 100 pages measured 133.8s in #595."""
    path = Path("configs/current-weekly-pilot-live.json")
    config = resolve_weekly_portfolio(json.loads(path.read_text(encoding="utf-8")), path)
    allowances = [
        capture_source_allowance_for(source, CAPTURE_SOURCE_TIMEOUT_SECONDS)
        for source in config["sources"]
        if RankingDetailRoute.from_source(source) is not None
    ]
    assert allowances
    assert min(allowances) >= MEASURED_CUSPIDE_SECONDS
    # And it is still a bound: nowhere near the (1 + max_pages) x timeout a
    # declared 100-page route could spend before issue #717.
    assert max(allowances) < 1010


class FakeSocket:
    """The socket under a response, which carries the timeout of one read."""

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        self.timeouts: list[float] = [timeout]

    def gettimeout(self) -> float:
        return self.timeout

    def settimeout(self, seconds: float) -> None:
        self.timeout = seconds
        self.timeouts.append(seconds)


class StallingResponse(FakeResponse):
    """A body whose slices arrive and then stop while the socket stays open.

    A socket keeps the timeout the request was made with, so a slice that
    lands just before the deadline can be followed by a read that blocks for
    that whole original timeout.  This response charges a stalled read exactly
    what the socket's current timeout allows, which is what makes narrowing it
    before each read observable.
    """

    def __init__(
        self,
        url: str,
        body: str,
        *,
        clock: FakeClock,
        request_timeout: float,
        seconds_per_slice: float,
        slice_bytes: int,
        slices_before_stall: int,
    ) -> None:
        super().__init__(url, body)
        self._clock = clock
        self._seconds_per_slice = seconds_per_slice
        self._slice_bytes = slice_bytes
        self._slices_before_stall = slices_before_stall
        self._offset = 0
        self.slices_served = 0
        self.socket = FakeSocket(request_timeout)
        self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=self.socket))

    def read1(self, limit: int = -1) -> bytes:
        if self.slices_served >= self._slices_before_stall:
            self._clock.advance(self.socket.timeout)
            raise TimeoutError("fake socket timeout")
        self._clock.advance(self._seconds_per_slice)
        self.slices_served += 1
        size = self._slice_bytes if limit < 0 else min(self._slice_bytes, limit)
        served = self._body[self._offset : self._offset + size]
        self._offset += len(served)
        return served

    def read(self, limit: int = -1) -> bytes:
        raise AssertionError("a streamed body must be read in slices")


def test_a_read_that_stalls_near_the_deadline_cannot_outlive_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The socket carries the time left, not the timeout the request began with."""
    clock = FakeClock()
    source = detail_route_source()
    # Every slice but the last of the allowance arrives, then the server stops
    # sending while the connection stays open.
    slices_before_stall = SLICES_BEFORE_DEADLINE - 1
    retry_slices_before_stall = RETRY_SLICES_BEFORE_DEADLINE - 1
    served: list[StallingResponse] = []
    body_bytes = len(list_html(DETAIL_ROWS).encode("utf-8"))

    def response_for(url: str, attempt: int) -> StallingResponse:
        retry = attempt > 1
        response = StallingResponse(
            url,
            list_html(DETAIL_ROWS),
            clock=clock,
            request_timeout=RETRY_SOCKET_SECONDS if retry else SOCKET_TIMEOUT_SECONDS,
            seconds_per_slice=DRIP_SLICE_SECONDS,
            slice_bytes=RETRY_SLICE_BYTES if retry else DRIP_SLICE_BYTES,
            slices_before_stall=retry_slices_before_stall if retry else slices_before_stall,
        )
        served.append(response)
        return response

    calls = install_response(monkeypatch, response_for)
    result = capture(tmp_path, clock, [source])

    assert calls == [source["url"], source["url"]]
    # The body still had slices to send, so the stall is a stalled read rather
    # than the end of the body, in each attempt.
    assert slices_before_stall * DRIP_SLICE_BYTES < body_bytes
    assert retry_slices_before_stall * RETRY_SLICE_BYTES < body_bytes
    assert served[0].slices_served == slices_before_stall
    assert served[1].slices_served == retry_slices_before_stall
    run = run_status(result, source["name"])
    first, retry = run["capture_attempts"]
    assert first["duration_seconds"] <= DETAIL_ALLOWANCE_SECONDS
    assert retry["duration_seconds"] <= RETRY_DETAIL_ALLOWANCE_SECONDS
    # Each read was held to the time left, so the stalled one cost the last
    # second of the allowance instead of the socket timeout it started with.
    # The retry's first read starts at its own ceiling and is narrowed the same way.
    assert served[0].socket.timeouts[0] == SOCKET_TIMEOUT_SECONDS
    assert served[0].socket.timeouts[-1] == DRIP_SLICE_SECONDS
    assert served[0].socket.timeouts[-1] < SOCKET_TIMEOUT_SECONDS
    assert served[1].socket.timeouts[0] == RETRY_SOCKET_SECONDS
    assert served[1].socket.timeouts[-1] == DRIP_SLICE_SECONDS
    assert run["status"] == "failed"
    assert run["reason"] == f"capture per-source bound exhausted after {RETRY_DETAIL_ALLOWANCE_SECONDS:.1f}s"
    assert first["reason"] == f"capture per-source bound exhausted after {DETAIL_ALLOWANCE_SECONDS:.1f}s"
    assert list((tmp_path / "run").glob("codex-capture-*.json")) == []


def test_the_allowance_follows_every_declared_page_not_only_the_detail_route() -> None:
    """A page declared under any key is still a page the one clock pays for.

    The allowance bounds every request a source makes, so a source that
    declares its pages as a paging sequence, an identity route or an Amazon
    Charts landing page has to earn the same room a detail route earns.
    Counting only the detail route held four live requests to the bound for
    one and failed them closed the first slow week (issue #717 review).
    """
    timeout = CAPTURE_SOURCE_TIMEOUT_SECONDS
    assert capture_source_allowance_for(paged_source(max_pages=4), timeout) == 4 * timeout
    assert capture_source_allowance_for(paged_source(max_pages=4, identity=True), timeout) == 5 * timeout
    assert capture_source_allowance_for(amazon_charts_source(), timeout) == 2 * timeout
    # A source that declares one page still earns exactly the bound it had.
    assert capture_source_allowance_for(paged_source(max_pages=1), timeout) == timeout
    assert capture_source_allowance_for(single_page_source(), timeout) == timeout


def test_the_stage_budget_follows_every_declared_page_too() -> None:
    """The stage budget and the per-source allowance read one declaration."""
    sources = [paged_source(max_pages=4, identity=True), amazon_charts_source(), single_page_source()]
    expected = CAPTURE_RUNTIME_BUDGET_SECONDS + 5 * CAPTURE_SOURCE_TIMEOUT_SECONDS
    assert capture_runtime_budget_for(sources) == expected
    assert capture_runtime_budget_for([single_page_source()]) == CAPTURE_RUNTIME_BUDGET_SECONDS


def test_no_configured_multi_request_source_is_held_to_one_page() -> None:
    """The live portfolio's paged, identity and Amazon Charts declarations."""
    path = Path("configs/current-weekly-portfolio.json")
    config = resolve_weekly_portfolio(json.loads(path.read_text(encoding="utf-8")), path)
    multi = [source for source in config["sources"] if declared_extra_request_count(source)]
    assert multi
    for source in multi:
        allowance = capture_source_allowance_for(source, CAPTURE_SOURCE_TIMEOUT_SECONDS)
        assert allowance > CAPTURE_SOURCE_TIMEOUT_SECONDS


def test_a_socket_is_never_widened_past_the_timeout_its_request_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clamp bounds one read; the allowance bounds the body, not a read.

    A source with minutes of allowance left reads pages fetched under a far
    smaller per-request clamp.  Narrowing the socket to the time the deadline
    has left would widen it past that clamp, and one stalled read would then
    spend the source's whole remaining clock (issue #717 review).
    """
    clock = FakeClock()
    source = detail_route_source()
    slices_before_stall = 2
    served: list[StallingResponse] = []

    def response_for(url: str, attempt: int) -> StallingResponse:
        retry = attempt > 1
        response = StallingResponse(
            url,
            list_html(DETAIL_ROWS),
            clock=clock,
            request_timeout=RETRY_SOCKET_SECONDS if retry else SOCKET_TIMEOUT_SECONDS,
            seconds_per_slice=DRIP_SLICE_SECONDS,
            slice_bytes=RETRY_SLICE_BYTES if retry else DRIP_SLICE_BYTES,
            slices_before_stall=slices_before_stall,
        )
        served.append(response)
        return response

    install_response(monkeypatch, response_for)
    result = capture(tmp_path, clock, [source])

    # The source had its whole detail allowance left when the first slice was
    # read, and the socket still carried the timeout its request was made with.
    assert DETAIL_ALLOWANCE_SECONDS > SOCKET_TIMEOUT_SECONDS
    assert max(served[0].socket.timeouts) == SOCKET_TIMEOUT_SECONDS
    # The retry's socket is never widened past its own ceiling either.
    assert max(served[1].socket.timeouts) == RETRY_SOCKET_SECONDS
    # So each stalled read cost one clamped timeout, not its attempt's allowance.
    run = run_status(result, source["name"])
    first, retry = run["capture_attempts"]
    assert first["duration_seconds"] <= slices_before_stall * DRIP_SLICE_SECONDS + SOCKET_TIMEOUT_SECONDS
    assert first["duration_seconds"] < DETAIL_ALLOWANCE_SECONDS
    assert retry["duration_seconds"] <= slices_before_stall * DRIP_SLICE_SECONDS + RETRY_SOCKET_SECONDS
    assert retry["duration_seconds"] < RETRY_DETAIL_ALLOWANCE_SECONDS
