"""Offline tests for issue #1005: one retry for a capture attempt that timed out or was rate-limited.

Every network call is an injected opener, the clock is a fake that only moves
when a fake request is served, and the sleep is a fake that advances that clock.
No test waits or makes a request.
"""

from __future__ import annotations

import json
import math
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

import autoscout.source_collectors as collectors
from autoscout.codex_recovery import ProductionCodexCaptureAdapter
from autoscout.runtime_budget import CAPTURE_SOURCE_RETRY_TIMEOUT_SECONDS
from autoscout.runtime_budget import capture_runtime_budget_for
from autoscout.runtime_budget import capture_source_allowance_for
from autoscout.source_collection import prepare_codex_extraction_worklist
from autoscout.source_collectors.agent_fetch import parse_retry_after

RUN_DATE = "2026-11-08"
REPORTING_PERIOD = "2026-11-01 - 2026-11-08"
# Socket ceiling the tests hand the adapter.  The retry ceiling is the shipped one.
SOCKET_TIMEOUT_SECONDS = 4.0
RETRY_SOCKET_SECONDS = CAPTURE_SOURCE_RETRY_TIMEOUT_SECONDS
ONE_PAGE_SECONDS = 1.0
DETAIL_ROWS = 11
DETAIL_MAX_PAGES = 11


class FakeClock:
    """A monotonic clock that only moves when a fake request or sleep is served."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSleep:
    """Records every requested wait and spends it on the fake clock."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self._clock.advance(seconds)


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


def single_page_source(name: str = "Retry source", url: str = "https://retry.test/ranking") -> dict[str, Any]:
    return {
        "name": name,
        "family": "bestseller/ranking",
        "url": url,
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


def detail_route_source(name: str = "Detail retry source") -> dict[str, Any]:
    source = single_page_source(name, "https://detail.test/ranking")
    source.update(
        {
            "ranking_evidence_url": "detail_page",
            "ranking_expected_raw_row_count": DETAIL_ROWS,
            "ranking_detail_link_selector": ".detail-link",
            "ranking_detail_url_pattern": r"https://[a-z.]+/books/[0-9]+",
            "ranking_detail_scope_selector": ".product-detail",
            "ranking_detail_fields": ["isbn", "category"],
            "ranking_detail_identifier_selector": ".isbn",
            "ranking_detail_identifier_pattern": r"ISBN:\s*(?P<identifier>97[89][0-9-]{10,20})",
            "ranking_detail_category_selector": ".category",
            "ranking_detail_category_pattern": r"Category:\s*(?P<category>[^<]+)",
            "ranking_detail_category_allowlist": ["Historia de América"],
            "ranking_detail_max_pages": DETAIL_MAX_PAGES,
            "eligible_candidate_category": "adult nonfiction",
            "ranking_identifier_selector": "",
            "ranking_identifier_attribute": "",
            "ranking_identifier_pattern": "",
        }
    )
    return source


def write_config(tmp_path: Path, sources: list[dict[str, Any]]) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"run_date": RUN_DATE, "sources": sources}, ensure_ascii=False), encoding="utf-8")
    return path


class ScriptedOpener:
    """Serve the list page from a script of steps and every detail page at a fixed cost.

    Steps, consumed one per list-page request:
    - ``("ok", seconds)``: serve the page after ``seconds``;
    - ``("timeout_after", seconds)``: the socket raises after ``seconds``;
    - ``("socket",)``: the socket waits out the timeout it was handed and raises;
    - ``("429", retry_after)``: an HTTP 429, with ``Retry-After`` when it is not None.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, clock: FakeClock, steps: list[tuple[Any, ...]]) -> None:
        self.clock = clock
        self.steps = list(steps)
        self.urls: list[str] = []
        self.timeouts: list[float] = []
        self.list_rows = 1
        monkeypatch.setattr(collectors, "urlopen", self._open)

    def _open(self, request: Any, timeout: float) -> FakeResponse:
        url = request.full_url
        self.urls.append(url)
        self.timeouts.append(timeout)
        tail = url.rsplit("/", 1)[-1]
        if tail.isdigit():
            self.clock.advance(ONE_PAGE_SECONDS)
            return FakeResponse(url, detail_html(int(tail)))
        step = self.steps.pop(0)
        kind = step[0]
        if kind == "ok":
            self.clock.advance(float(step[1]))
            return FakeResponse(url, list_html(self.list_rows))
        if kind == "timeout_after":
            self.clock.advance(float(step[1]))
            raise TimeoutError("fake socket timeout")
        if kind == "socket":
            self.clock.advance(timeout)
            raise TimeoutError("fake socket timeout")
        if kind == "429":
            self.clock.advance(0.5)
            headers = Message()
            if step[1] is not None:
                headers["Retry-After"] = str(step[1])
            raise HTTPError(url, 429, "Too Many Requests", headers, None)  # type: ignore[arg-type]
        raise AssertionError(f"unknown step {step!r}")


def capture(
    tmp_path: Path,
    clock: FakeClock,
    sleep: FakeSleep,
    sources: list[dict[str, Any]],
    *,
    runtime_budget_seconds: float | None = None,
    retry_source_timeout_seconds: float = RETRY_SOCKET_SECONDS,
) -> dict[str, Any]:
    config = write_config(tmp_path, sources)
    options: dict[str, Any] = {
        "per_source_timeout_seconds": SOCKET_TIMEOUT_SECONDS,
        "retry_source_timeout_seconds": retry_source_timeout_seconds,
        "sleep": sleep,
    }
    if runtime_budget_seconds is not None:
        options["runtime_budget_seconds"] = runtime_budget_seconds
    adapter = ProductionCodexCaptureAdapter(clock, **options)
    return adapter.prepare(config, tmp_path / "run", live=True, effective_date=RUN_DATE)


def statuses(result: dict[str, Any]) -> list[str]:
    return [run["status"] for run in result["source_runs"]]


def run_for(result: dict[str, Any], name: str) -> dict[str, Any]:
    return next(run for run in result["source_runs"] if run["source_name"] == name)


def snapshot_rows(tmp_path: Path) -> list[dict[str, Any]]:
    snapshot = json.loads((tmp_path / "run" / "progress-snapshot.json").read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = snapshot["rows"]
    return rows


# --- Retry-After parsing -------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "date_header", "expected"),
    [
        ("5", None, 5),
        ("Sun, 08 Nov 2026 12:00:05 GMT", "Sun, 08 Nov 2026 12:00:00 GMT", 5),
        ("Sun, 08 Nov 2026 12:00:05 GMT", None, None),
        ("garbage", "Sun, 08 Nov 2026 12:00:00 GMT", None),
        ("-1", None, None),
    ],
)
def test_parse_retry_after(value, date_header, expected):
    assert parse_retry_after(value, date_header) == expected


# --- The one retry -------------------------------------------------------------


def test_a_request_timeout_is_retried_once_and_the_retry_captures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock = FakeClock()
    sleep = FakeSleep(clock)
    source = single_page_source()
    opener = ScriptedOpener(monkeypatch, clock, [("timeout_after", 1.0), ("ok", ONE_PAGE_SECONDS)])

    result = capture(tmp_path, clock, sleep, [source])

    run = run_for(result, source["name"])
    assert run["status"] == "codex-captured"
    assert [attempt["status"] for attempt in run["capture_attempts"]] == ["failed", "codex-captured"]
    assert [attempt["safe_failure_status"] for attempt in run["capture_attempts"]] == ["request_timeout", ""]
    assert statuses(result).count("codex-captured") == 1
    assert statuses(result).count("failed") == 0
    assert len(opener.urls) == 2
    assert sleep.calls == []
    worklist = json.loads((tmp_path / "run" / "codex-extraction-worklist.json").read_text(encoding="utf-8"))
    assert [item["source_name"] for item in worklist["work_items"]] == [source["name"]]


def test_a_capture_deadline_is_retried_once_and_the_retry_captures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock = FakeClock()
    sleep = FakeSleep(clock)
    source = single_page_source()
    # The first request spends its whole allowance, which is the per-source bound.
    ScriptedOpener(monkeypatch, clock, [("timeout_after", SOCKET_TIMEOUT_SECONDS), ("ok", ONE_PAGE_SECONDS)])

    result = capture(tmp_path, clock, sleep, [source])

    run = run_for(result, source["name"])
    assert run["status"] == "codex-captured"
    assert run["capture_attempts"][0]["safe_failure_status"] == "capture_deadline"
    assert run["capture_attempts"][1]["status"] == "codex-captured"
    assert statuses(result).count("codex-captured") == 1
    assert statuses(result).count("failed") == 0
    rows = snapshot_rows(tmp_path)
    assert rows[0]["state"] == "finished"
    assert rows[0]["outcome"] == "codex-captured"


def test_no_room_in_the_stage_budget_means_no_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock = FakeClock()
    sleep = FakeSleep(clock)
    source = single_page_source()
    opener = ScriptedOpener(monkeypatch, clock, [("timeout_after", SOCKET_TIMEOUT_SECONDS), ("ok", 1.0)])

    result = capture(tmp_path, clock, sleep, [source], runtime_budget_seconds=5.0)

    run = run_for(result, source["name"])
    assert run["status"] == "failed"
    assert len(run["capture_attempts"]) == 1
    assert len(opener.urls) == 1
    assert sleep.calls == []
    assert statuses(result).count("failed") == 1


def test_the_retry_is_clamped_to_the_budget_that_is_left(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock = FakeClock()
    sleep = FakeSleep(clock)
    source = single_page_source()
    budget = 10.0
    # The first attempt times out after one second; the retry's socket then
    # waits out whatever the stage has left, and no more.
    ScriptedOpener(monkeypatch, clock, [("timeout_after", 1.0), ("socket",)])

    result = capture(tmp_path, clock, sleep, [source], runtime_budget_seconds=budget)

    run = run_for(result, source["name"])
    second = run["capture_attempts"][1]
    assert second["socket_timeout_seconds"] == pytest.approx(budget - 1.0)
    assert second["allowance_seconds"] == pytest.approx(budget - 1.0)
    assert clock.now <= budget
    assert run["duration_seconds"] <= budget
    assert run["status"] == "failed"
    assert result["duration_seconds"] <= budget


def test_a_second_timeout_is_final(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock = FakeClock()
    sleep = FakeSleep(clock)
    source = single_page_source()
    opener = ScriptedOpener(monkeypatch, clock, [("timeout_after", 1.0), ("timeout_after", 1.0)])

    result = capture(tmp_path, clock, sleep, [source])

    run = run_for(result, source["name"])
    assert run["status"] == "failed"
    assert [attempt["safe_failure_status"] for attempt in run["capture_attempts"]] == [
        "request_timeout",
        "request_timeout",
    ]
    assert len(opener.urls) == 2
    assert statuses(result).count("failed") == 1
    assert statuses(result).count("codex-captured") == 0


def test_a_declared_detail_route_retry_gets_its_declared_allowance_clamped_to_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = FakeClock()
    sleep = FakeSleep(clock)
    source = detail_route_source()
    budget = capture_runtime_budget_for([source])
    opener = ScriptedOpener(monkeypatch, clock, [("timeout_after", 1.0), ("ok", 1.0)])
    opener.list_rows = DETAIL_ROWS

    result = capture(tmp_path, clock, sleep, [source], runtime_budget_seconds=budget)

    run = run_for(result, source["name"])
    assert run["status"] == "codex-captured"
    second = run["capture_attempts"][1]
    expected = min(capture_source_allowance_for(source, RETRY_SOCKET_SECONDS), budget - 1.0)
    assert second["allowance_seconds"] == pytest.approx(expected)
    assert second["socket_timeout_seconds"] == pytest.approx(min(RETRY_SOCKET_SECONDS, budget - 1.0))


# --- HTTP 429 and Retry-After --------------------------------------------------


def test_a_429_with_retry_after_in_budget_waits_then_retries_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock = FakeClock()
    sleep = FakeSleep(clock)
    source = single_page_source()
    opener = ScriptedOpener(monkeypatch, clock, [("429", 5), ("ok", ONE_PAGE_SECONDS)])

    result = capture(tmp_path, clock, sleep, [source])

    run = run_for(result, source["name"])
    assert run["status"] == "codex-captured"
    assert len(opener.urls) == 2
    # The 429 was received at 0.5s and the server asked for five seconds from then.
    assert sleep.calls == [pytest.approx(5.0)]
    assert run["capture_attempts"][0]["status"] == "skipped"
    assert run["capture_attempts"][1]["waited_seconds"] == pytest.approx(5.0)
    assert run["duration_seconds"] == pytest.approx(0.5 + 5.0 + ONE_PAGE_SECONDS)
    assert statuses(result).count("codex-captured") == 1
    assert statuses(result).count("skipped") == 0


def test_long_retry_after_publishes_wait_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock = FakeClock()
    snapshots: list[dict[str, Any]] = []

    class ObservingSleep(FakeSleep):
        def __call__(self, seconds: float) -> None:
            super().__call__(seconds)
            snapshots.append(json.loads((tmp_path / "run" / "progress-snapshot.json").read_text(encoding="utf-8")))

    sleep = ObservingSleep(clock)
    source = single_page_source()
    ScriptedOpener(monkeypatch, clock, [("429", 240), ("ok", ONE_PAGE_SECONDS)])
    result = capture(tmp_path, clock, sleep, [source], runtime_budget_seconds=590.0)

    assert sleep.calls == [60.0, 60.0, 60.0, 60.0]
    assert sum(sleep.calls) == 240.0
    rows = [snapshot["rows"][0] for snapshot in snapshots]
    assert all(row["state"] == "in_flight" for row in rows)
    assert [row["elapsed_seconds"] for row in rows] == [0.0, 60.0, 120.0, 180.0]
    assert [row["remaining_runtime_budget_seconds"] for row in rows] == [589.5, 529.5, 469.5, 409.5]
    assert run_for(result, source["name"])["capture_attempts"][1]["waited_seconds"] == 240.0
    assert statuses(result).count("codex-captured") == 1
    assert statuses(result).count("skipped") == 0
    assert snapshot_rows(tmp_path)[0]["state"] == "finished"


def test_a_429_without_retry_after_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock = FakeClock()
    sleep = FakeSleep(clock)
    source = single_page_source()
    opener = ScriptedOpener(monkeypatch, clock, [("429", None), ("ok", ONE_PAGE_SECONDS)])

    result = capture(tmp_path, clock, sleep, [source])

    run = run_for(result, source["name"])
    assert run["status"] == "skipped"
    assert run["debug_metadata"]["safe_failure_status"] == "source_blocked"
    assert run["debug_metadata"]["rate_limited"] is True
    assert "retry_after_seconds" not in run["debug_metadata"]
    assert "capture_attempts" not in run
    assert len(opener.urls) == 1
    assert sleep.calls == []
    assert statuses(result).count("skipped") == 1


def test_a_429_whose_retry_after_outlasts_the_budget_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock = FakeClock()
    sleep = FakeSleep(clock)
    source = single_page_source()
    opener = ScriptedOpener(monkeypatch, clock, [("429", 60), ("ok", ONE_PAGE_SECONDS)])

    result = capture(tmp_path, clock, sleep, [source], runtime_budget_seconds=20.0)

    run = run_for(result, source["name"])
    assert run["status"] == "skipped"
    assert run["debug_metadata"]["retry_after_seconds"] == 60.0
    assert len(opener.urls) == 1
    assert sleep.calls == []
    assert clock.now <= 20.0


# --- Evidence, order and the product rules that must not move -----------------


def test_a_retried_source_keeps_its_config_order_in_the_published_source_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = FakeClock()
    sleep = FakeSleep(clock)
    first = single_page_source("First", "https://first.test/ranking")
    second = single_page_source("Second", "https://second.test/ranking")
    ScriptedOpener(monkeypatch, clock, [("timeout_after", 1.0), ("ok", 1.0), ("ok", 1.0)])

    result = capture(tmp_path, clock, sleep, [first, second])

    assert [run["source_name"] for run in result["source_runs"]] == ["First", "Second"]
    assert result["source_runs"][0]["status"] == "codex-captured"
    assert result["source_runs"][1]["status"] == "codex-captured"
    published = json.loads((tmp_path / "run" / "source-runs.json").read_text(encoding="utf-8"))
    assert [run["source_name"] for run in published] == ["First", "Second"]
    assert len(published[0]["capture_attempts"]) == 2


def test_the_retry_timeout_must_be_finite_and_at_least_the_per_source_timeout(tmp_path: Path):
    source = single_page_source()
    config = write_config(tmp_path, [source])
    with pytest.raises(ValueError):
        prepare_codex_extraction_worklist(
            config, tmp_path / "out", RUN_DATE, per_source_timeout_seconds=10, retry_source_timeout_seconds=5
        )
    with pytest.raises(ValueError):
        prepare_codex_extraction_worklist(config, tmp_path / "out", RUN_DATE, retry_source_timeout_seconds=math.inf)
    with pytest.raises(ValueError):
        ProductionCodexCaptureAdapter(per_source_timeout_seconds=10, retry_source_timeout_seconds=5)
