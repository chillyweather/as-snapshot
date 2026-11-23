"""Offline acceptance tests for the Issue #335 Source Candidate probe."""

from __future__ import annotations

import json
import stat
import subprocess
from datetime import date
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pytest

from autoscout.__main__ import main
from autoscout.http_decoding import MAX_RESPONSE_BYTES
from autoscout.source_probe import SourceCandidateLedgerError
from autoscout.source_probe import run_source_candidate_probe

ROOT = Path(__file__).resolve().parents[1]


SOURCE_URL = "https://example.test/list"


def _page(*, entries: int = 2, javascript: bool = False) -> bytes:
    marker = '<script id="__NEXT_DATA__">{"props":{}}</script>' if javascript else ""
    items = "".join(
        f'<li><a href="{SOURCE_URL}/book-{index}">Book {index}</a><span class="author">By: Jane Author</span></li>'
        for index in range(1, entries + 1)
    )
    return f"<html><body><h1>Current list</h1>{marker}<ul>{items}</ul></body></html>".encode()


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"", url: str = SOURCE_URL) -> None:
        self.status = status
        self.body = body
        self.url = url
        self.read_sizes: list[int] = []
        self.closed = False

    def geturl(self) -> str:
        return self.url

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        return self.body

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(self, plans: dict[str, list[Any]]) -> None:
        self.plans = {url: list(responses) for url, responses in plans.items()}
        self.calls: list[tuple[str, str]] = []
        self.responses: list[FakeResponse] = []

    def __call__(self, request: Any, timeout: float) -> FakeResponse:
        method = request.get_method()
        url = request.full_url
        self.calls.append((method, url))
        try:
            value = self.plans[url].pop(0)
        except (KeyError, IndexError) as exc:
            raise AssertionError(f"unexpected network call: {method} {url}") from exc
        if isinstance(value, BaseException):
            raise value
        response = value
        self.responses.append(response)
        return response


def _ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "ledger" / "source-candidates.json"


def _accepted_plan(url: str = SOURCE_URL) -> dict[str, list[FakeResponse]]:
    return {url: [FakeResponse(200), FakeResponse(200, _page())]}


def test_clean_accept_reports_grounded_evidence_and_exit_zero(tmp_path: Path) -> None:
    transport = FakeTransport(_accepted_plan())
    report, code = run_source_candidate_probe([SOURCE_URL], ledger_path=_ledger_path(tmp_path), opener=transport)

    result = report["candidates"][0]
    assert code == 0
    assert result["outcome"] == "accept"
    assert result["reason_code"] == "ok"
    assert result["head_status"] == 200
    assert result["get_status"] == 200
    assert result["detected_heading"] == "Current list"
    assert result["bounded_entry_count"] == 2
    assert result["samples"][0] == {
        "title": "Book 1",
        "author": "Jane Author",
        "url": f"{SOURCE_URL}/book-1",
    }
    assert [method for method, _url in transport.calls] == ["HEAD", "GET"]


def test_default_probe_report_omits_visible_title_selector(tmp_path: Path) -> None:
    report, code = run_source_candidate_probe(
        [SOURCE_URL], ledger_path=_ledger_path(tmp_path), opener=FakeTransport(_accepted_plan())
    )

    assert code == 0
    assert "visible_title_selector" not in report["candidates"][0]


def test_default_retained_capture_omits_visible_title_selector(tmp_path: Path) -> None:
    captures = tmp_path / "captures"
    report, code = run_source_candidate_probe(
        [SOURCE_URL],
        keep_captures=captures,
        ledger_path=_ledger_path(tmp_path),
        opener=FakeTransport(_accepted_plan()),
    )

    assert code == 0
    assert report["candidates"][0]["outcome"] == "accept"
    capture = json.loads(next(captures.glob("*.json")).read_text(encoding="utf-8"))
    assert "visible_title_selector" not in capture


def test_default_duplicate_probe_report_omits_visible_title_selector(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, opener=FakeTransport(_accepted_plan()))
    report, code = run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, opener=FakeTransport({}))

    assert code == 0
    assert "visible_title_selector" not in report["candidates"][0]


def test_explicit_visible_title_selector_is_reported_and_retained(tmp_path: Path) -> None:
    html = (
        b'<html><body><div class="pn-editoria-titulo">Visible list</div><ul>'
        b'<li><a href="https://example.test/list/book-1">Book One</a>'
        b'<span class="author">Jane Author</span></li></ul></body></html>'
    )
    captures = tmp_path / "captures"
    report, code = run_source_candidate_probe(
        [SOURCE_URL],
        visible_title_selectors=[".pn-editoria-titulo"],
        keep_captures=captures,
        ledger_path=_ledger_path(tmp_path),
        opener=FakeTransport({SOURCE_URL: [FakeResponse(200), FakeResponse(200, html)]}),
    )

    assert code == 0
    assert report["candidates"][0]["visible_title_selector"] == ".pn-editoria-titulo"
    capture = json.loads(next(captures.glob("*.json")).read_text(encoding="utf-8"))
    assert capture["visible_title_selector"] == ".pn-editoria-titulo"


def test_all_defer_all_reject_and_mixed_exit_codes(tmp_path: Path) -> None:
    defer_url = "https://example.test/defer"
    reject_url = "https://example.test/reject"
    defer_transport = FakeTransport(
        {
            defer_url: [
                FakeResponse(200, url=defer_url),
                FakeResponse(200, _page(entries=0, javascript=True), defer_url),
            ],
        }
    )
    report, code = run_source_candidate_probe([defer_url], ledger_path=tmp_path / "defer.json", opener=defer_transport)
    assert code == 3
    assert report["candidates"][0]["outcome"] == "defer"

    reject_transport = FakeTransport({reject_url: [FakeResponse(403)]})
    report, code = run_source_candidate_probe(
        [reject_url], ledger_path=tmp_path / "reject.json", opener=reject_transport
    )
    assert code == 1
    assert report["candidates"][0]["outcome"] == "reject"

    mixed_transport = FakeTransport({**_accepted_plan(), reject_url: [FakeResponse(429)]})
    report, code = run_source_candidate_probe(
        [reject_url, SOURCE_URL], ledger_path=tmp_path / "mixed.json", opener=mixed_transport
    )
    assert code == 0
    assert [candidate["outcome"] for candidate in report["candidates"]] == ["reject", "accept"]


def test_malformed_url_does_not_isolate_other_candidates(tmp_path: Path) -> None:
    transport = FakeTransport(_accepted_plan())
    report, code = run_source_candidate_probe(
        ["not a url", SOURCE_URL], ledger_path=_ledger_path(tmp_path), opener=transport
    )

    assert code == 0
    assert report["candidates"][0]["reason_code"] == "invalid_url"
    assert report["candidates"][0]["head_status"] is None
    assert report["candidates"][1]["outcome"] == "accept"
    assert transport.calls == [("HEAD", SOURCE_URL), ("GET", SOURCE_URL)]


@pytest.mark.parametrize("status", [403, 429, 500, 502, 503])
def test_denied_or_error_head_never_gets(tmp_path: Path, status: int) -> None:
    url = f"https://example.test/{status}"
    transport = FakeTransport({url: [FakeResponse(status, url=url)]})
    report, code = run_source_candidate_probe([url], ledger_path=tmp_path / f"{status}.json", opener=transport)

    assert code == 1
    assert report["candidates"][0]["reason_code"] == "head_denied"
    assert [method for method, _url in transport.calls] == ["HEAD"]


@pytest.mark.parametrize("status", [405, 501])
def test_head_method_not_supported_gets_exactly_once(tmp_path: Path, status: int) -> None:
    url = f"https://example.test/{status}"
    transport = FakeTransport({url: [FakeResponse(status, url=url), FakeResponse(200, _page(), url=url)]})
    report, code = run_source_candidate_probe([url], ledger_path=tmp_path / f"{status}.json", opener=transport)

    assert code == 0
    assert report["candidates"][0]["outcome"] == "accept"
    assert [method for method, _url in transport.calls] == ["HEAD", "GET"]


def test_head_unrelated_redirect_rejects_before_get(tmp_path: Path) -> None:
    transport = FakeTransport({SOURCE_URL: [FakeResponse(200, url="https://other.test/list")]})
    report, code = run_source_candidate_probe([SOURCE_URL], ledger_path=tmp_path / "ledger.json", opener=transport)

    assert code == 1
    assert report["candidates"][0]["reason_code"] == "unrelated_redirect"
    assert [method for method, _url in transport.calls] == ["HEAD"]


def test_head_trailing_slash_normalization_still_gets_once(tmp_path: Path) -> None:
    transport = FakeTransport(
        {SOURCE_URL: [FakeResponse(200, url=f"{SOURCE_URL}/"), FakeResponse(200, _page(), f"{SOURCE_URL}/")]}
    )
    report, code = run_source_candidate_probe([SOURCE_URL], ledger_path=tmp_path / "ledger.json", opener=transport)

    assert code == 0
    assert report["candidates"][0]["outcome"] == "accept"
    assert [method for method, _url in transport.calls] == ["HEAD", "GET"]


def test_get_http_failure_and_transport_failure_are_isolated(tmp_path: Path) -> None:
    http_url = "https://example.test/get-http-error"
    transport_url = "https://example.test/get-transport-error"
    transport = FakeTransport(
        {
            http_url: [FakeResponse(200, url=http_url), FakeResponse(500, url=http_url)],
            transport_url: [FakeResponse(200, url=transport_url), URLError("offline fake")],
        }
    )
    report, code = run_source_candidate_probe(
        [http_url, transport_url], ledger_path=_ledger_path(tmp_path), opener=transport
    )

    assert code == 1
    assert report["candidates"][0]["reason_code"] == "http_error"
    assert report["candidates"][1]["reason_code"] == "get_request_failed"
    assert len(transport.calls) == 4


def test_bot_wall_and_unrelated_redirect_are_rejected_without_captures(tmp_path: Path) -> None:
    bot_url = "https://example.test/bot"
    redirect_url = "https://example.test/redirect"
    bot_html = b"<html><body><h1>Verify you are human</h1><p>captcha challenge</p></body></html>"
    transport = FakeTransport(
        {
            bot_url: [FakeResponse(200, url=bot_url), FakeResponse(200, bot_html, bot_url)],
            redirect_url: [
                FakeResponse(200, url=redirect_url),
                FakeResponse(200, _page(), "https://other.test/list"),
            ],
        }
    )
    captures = tmp_path / "captures"
    report, code = run_source_candidate_probe(
        [bot_url, redirect_url],
        keep_captures=captures,
        ledger_path=_ledger_path(tmp_path),
        opener=transport,
    )

    assert code == 1
    assert [candidate["reason_code"] for candidate in report["candidates"]] == [
        "blocked_response",
        "unrelated_redirect",
    ]
    assert not captures.exists()


def test_body_reads_are_bounded_and_oversize_evidence_defers(tmp_path: Path) -> None:
    transport = FakeTransport(
        {
            SOURCE_URL: [FakeResponse(200), FakeResponse(200, b"<h1>List</h1>" + b"x" * (MAX_RESPONSE_BYTES + 10))],
        }
    )
    report, code = run_source_candidate_probe([SOURCE_URL], ledger_path=_ledger_path(tmp_path), opener=transport)

    assert code == 3
    assert report["candidates"][0]["reason_code"] == "response_too_large"
    assert transport.responses[-1].read_sizes == [MAX_RESPONSE_BYTES + 1]


def test_captures_are_discarded_by_default_and_retained_only_for_accept(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    transport = FakeTransport(_accepted_plan())
    report, code = run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, opener=transport)
    assert code == 0
    assert not (tmp_path / "captures").exists()
    assert "capture_file" not in report["candidates"][0]

    keep = tmp_path / "captures"
    transport = FakeTransport(_accepted_plan())
    report, code = run_source_candidate_probe(
        [SOURCE_URL], keep_captures=keep, ledger_path=tmp_path / "kept-ledger.json", opener=transport
    )
    assert code == 0
    capture_files = list(keep.glob("*.json"))
    assert len(capture_files) == 1
    capture = json.loads(capture_files[0].read_text(encoding="utf-8"))
    assert capture["collection_mode"] == "source-candidate-probe"
    assert capture["source_page"] == SOURCE_URL
    assert capture["html"]
    assert len(capture["html"].encode()) <= MAX_RESPONSE_BYTES


def test_accepted_capture_respects_utf8_byte_bound_at_partial_multibyte_boundary(tmp_path: Path) -> None:
    page = _page()
    body = page + b"x" * (MAX_RESPONSE_BYTES - len(page) - 2) + b"\xe2\x82"
    assert len(body) == MAX_RESPONSE_BYTES
    transport = FakeTransport({SOURCE_URL: [FakeResponse(200), FakeResponse(200, body)]})
    keep = tmp_path / "captures"

    report, code = run_source_candidate_probe(
        [SOURCE_URL], keep_captures=keep, ledger_path=tmp_path / "ledger.json", opener=transport
    )

    assert code == 0
    assert report["candidates"][0]["outcome"] == "accept"
    capture = json.loads(next(keep.glob("*.json")).read_text(encoding="utf-8"))
    assert len(capture["html"].encode("utf-8")) <= MAX_RESPONSE_BYTES


def test_retained_capture_redacts_csrf_and_preserves_book_evidence(tmp_path: Path) -> None:
    html = (
        b'<meta name="csrf-token" content="csrf-secret-value">'
        b'<h1>Current list</h1><ul><li><a href="https://example.test/book">Token Economy</a>'
        b'<span class="author">By: Jane Author</span></li></ul>'
    )
    transport = FakeTransport({SOURCE_URL: [FakeResponse(200), FakeResponse(200, html)]})
    keep = tmp_path / "captures"
    report, code = run_source_candidate_probe(
        [SOURCE_URL], keep_captures=keep, ledger_path=tmp_path / "ledger.json", opener=transport
    )

    assert code == 0
    assert report["candidates"][0]["outcome"] == "accept"
    capture = json.loads(next(keep.glob("*.json")).read_text(encoding="utf-8"))
    assert "csrf-secret-value" not in capture["html"]
    assert "REDACTED" in capture["html"]
    assert "Token Economy" in capture["html"]
    assert "https://example.test/book" in capture["html"]


def test_ledger_is_deterministic_and_duplicate_is_no_network(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    second = "https://example.test/second"
    transport = FakeTransport({**_accepted_plan(), second: [FakeResponse(200), FakeResponse(200, _page(), second)]})
    report, code = run_source_candidate_probe([second, SOURCE_URL], ledger_path=ledger, opener=transport)
    assert code == 0
    assert len(report["candidates"]) == 2

    data = json.loads(ledger.read_text(encoding="utf-8"))
    assert all(date.fromisoformat(entry["probe_date"]) for entry in data["entries"])
    dates_by_url = {entry["url"]: entry["probe_date"] for entry in data["entries"]}
    assert report["candidates"][0]["probe_date"] == dates_by_url[second]
    assert [entry["normalized_url"] for entry in data["entries"]] == sorted(
        entry["normalized_url"] for entry in data["entries"]
    )
    markdown = (ledger.with_suffix(".md")).read_text(encoding="utf-8")
    assert markdown.index(SOURCE_URL) < markdown.index(second)

    class NoNetwork:
        def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("ledger duplicate made a network request")

    report, code = run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, opener=NoNetwork())
    assert code == 0
    assert report["candidates"][0]["reason_code"] == "already_evaluated"
    assert report["candidates"][0]["prior_outcome"] == "accept"
    assert report["candidates"][0]["probe_date"] == dates_by_url[SOURCE_URL]
    assert report["candidates"][0]["probe_date"] == data["entries"][0]["probe_date"]

    duplicate_ledger = tmp_path / "duplicate-ledger.json"
    duplicate_data = dict(data)
    duplicate_data["entries"] = [*list(data["entries"]), dict(data["entries"][0])]
    duplicate_ledger.write_text(json.dumps(duplicate_data), encoding="utf-8")
    with pytest.raises(SourceCandidateLedgerError, match="duplicate normalized_url"):
        run_source_candidate_probe([SOURCE_URL], ledger_path=duplicate_ledger, opener=NoNetwork())

    missing_section_ledger = tmp_path / "missing-http-section.json"
    missing_section_data = dict(data)
    missing_section_entry = dict(data["entries"][0])
    missing_section_evidence = dict(missing_section_entry["http_evidence"])
    del missing_section_evidence["get"]
    missing_section_entry["http_evidence"] = missing_section_evidence
    missing_section_data["entries"] = [missing_section_entry]
    missing_section_ledger.write_text(json.dumps(missing_section_data), encoding="utf-8")
    with pytest.raises(SourceCandidateLedgerError, match="head and get sections"):
        run_source_candidate_probe([SOURCE_URL], ledger_path=missing_section_ledger, opener=NoNetwork())


def test_legacy_minimal_ledger_is_rejected_before_network(tmp_path: Path) -> None:
    ledger = tmp_path / "legacy.json"
    original = json.dumps({"version": 1, "entries": [{"url": SOURCE_URL, "outcome": "defer"}]}).encode("utf-8")
    ledger.write_bytes(original)

    class NoNetwork:
        def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("legacy ledger reached the network")

    with pytest.raises(SourceCandidateLedgerError):
        run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, opener=NoNetwork())
    assert ledger.read_bytes() == original


@pytest.mark.parametrize(
    "ledger_data",
    [
        [{"url": SOURCE_URL, "outcome": "accept"}],
        {"version": 1, "entries": [{"source_page": SOURCE_URL, "outcome": "accept"}]},
        {"version": 1, "entries": [{"url": SOURCE_URL, "status": "accept"}]},
        {"version": 1, "entries": [{"url": SOURCE_URL, "outcome": "unknown"}]},
    ],
)
def test_ledger_schema_rejects_legacy_aliases_and_unknown_outcomes_before_network(
    tmp_path: Path, ledger_data: Any
) -> None:
    ledger = tmp_path / "invalid.json"
    original = json.dumps(ledger_data).encode("utf-8")
    ledger.write_bytes(original)

    class NoNetwork:
        def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("invalid ledger reached the network")

    with pytest.raises(SourceCandidateLedgerError):
        run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, opener=NoNetwork())
    assert ledger.read_bytes() == original


def test_protected_paths_and_existing_config_are_contained_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    protected_names = (
        "config.json",
        "source-mentions.json",
        "scouting-candidates.json",
        "candidate-history.json",
        "candidate-visibility.json",
        "editorial-profile.json",
        "weekly-config.json",
    )
    protected_files = {tmp_path / name: f"protected:{name}\n".encode() for name in protected_names}
    for path, content in protected_files.items():
        path.write_bytes(content)
    before = {path: path.read_bytes() for path in protected_files}
    network_calls: list[Any] = []

    class NoNetwork:
        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            network_calls.append((args, kwargs))
            raise AssertionError("protected path reached the network")

    monkeypatch.setattr("autoscout.source_probe.urlopen", NoNetwork())
    for path in protected_files:
        code = main(
            [
                "probe-source-candidate",
                "--url",
                SOURCE_URL,
                "--ledger",
                str(path),
            ]
        )
        report = json.loads(capsys.readouterr().out)
        assert code == 2
        assert "error" in report

    assert network_calls == []
    assert {path: path.read_bytes() for path in protected_files} == before


def test_protected_weekly_config_and_state_directories_are_contained_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    locations = (
        Path("configs") / "ledger.json",
        Path("runs") / "ledger.json",
        Path("state") / "ledger.json",
        Path("runs") / "current-weekly-pilot-russian" / "ledger.json",
    )
    network_calls: list[Any] = []

    class NoNetwork:
        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            network_calls.append((args, kwargs))
            raise AssertionError("protected directory reached the network")

    monkeypatch.setattr("autoscout.source_probe.urlopen", NoNetwork())
    for relative_path in locations:
        ledger = tmp_path / relative_path
        ledger.parent.mkdir(parents=True, exist_ok=True)
        code = main(
            [
                "probe-source-candidate",
                "--url",
                SOURCE_URL,
                "--ledger",
                str(ledger),
            ]
        )
        report = json.loads(capsys.readouterr().out)
        assert code == 2
        assert "error" in report
        assert not ledger.exists()

    assert network_calls == []


def test_keep_captures_protected_weekly_state_is_rejected_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    state = tmp_path / "runs" / "current-weekly-pilot-russian"
    state.mkdir(parents=True)
    protected = state / "candidate-history.json"
    protected.write_bytes(b"protected weekly state\n")
    before = protected.read_bytes()
    network_calls: list[Any] = []

    class NoNetwork:
        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            network_calls.append((args, kwargs))
            raise AssertionError("protected capture directory reached the network")

    monkeypatch.setattr("autoscout.source_probe.urlopen", NoNetwork())
    code = main(
        [
            "probe-source-candidate",
            "--url",
            SOURCE_URL,
            "--keep-captures",
            str(state),
            "--ledger",
            str(tmp_path / "safe-ledger.json"),
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert code == 2
    assert "error" in report
    assert network_calls == []
    assert protected.read_bytes() == before
    assert list(state.glob("source-candidate-*.json")) == []


def test_missing_nested_capture_through_alias_to_protected_directory_is_rejected_before_network(
    tmp_path: Path,
) -> None:
    protected_dir = tmp_path / "runs"
    protected_dir.mkdir()
    protected = protected_dir / "candidate-history.json"
    protected.write_bytes(b"protected weekly state\n")
    alias = tmp_path / "safe-alias"
    alias.symlink_to(protected_dir, target_is_directory=True)
    before = protected.read_bytes()

    class NoNetwork:
        def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("protected capture directory reached the network")

    with pytest.raises(SourceCandidateLedgerError):
        run_source_candidate_probe(
            [SOURCE_URL],
            keep_captures=alias / "nested" / "captures",
            ledger_path=tmp_path / "safe-ledger.json",
            opener=NoNetwork(),
        )

    assert protected.read_bytes() == before
    assert not (protected_dir / "nested").exists()


def test_missing_nested_ledger_through_alias_to_protected_directory_is_rejected_before_network(
    tmp_path: Path,
) -> None:
    protected_dir = tmp_path / "runs"
    protected_dir.mkdir()
    protected = protected_dir / "candidate-history.json"
    protected.write_bytes(b"protected weekly state\n")
    alias = tmp_path / "safe-alias"
    alias.symlink_to(protected_dir, target_is_directory=True)
    before = protected.read_bytes()

    class NoNetwork:
        def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("protected ledger directory reached the network")

    with pytest.raises(SourceCandidateLedgerError):
        run_source_candidate_probe(
            [SOURCE_URL],
            ledger_path=alias / "nested" / "ledger.json",
            opener=NoNetwork(),
        )

    assert protected.read_bytes() == before
    assert not (protected_dir / "nested").exists()


def test_ledger_markdown_sidecar_rejects_symlink_target_before_network(tmp_path: Path) -> None:
    protected = tmp_path / "editorial-profile.json"
    original = b"protected state\n"
    protected.write_bytes(original)
    sidecar = tmp_path / "ledger.md"
    sidecar.symlink_to(protected)

    class NoNetwork:
        def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("unsafe sidecar reached the network")

    with pytest.raises(SourceCandidateLedgerError):
        run_source_candidate_probe([SOURCE_URL], ledger_path=tmp_path / "ledger.json", opener=NoNetwork())
    assert protected.read_bytes() == original


def test_probe_cli_emits_json_and_does_not_touch_weekly_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    ledger = _ledger_path(tmp_path)
    transport = FakeTransport(_accepted_plan())
    monkeypatch.setattr("autoscout.source_probe.urlopen", transport)
    state = tmp_path / "weekly-state"
    state.mkdir()
    state_files = {
        state / "candidate-history.json": b"[]\n",
        state / "candidate-visibility.json": b"{}\n",
        state / "editorial-profile.json": b"[]\n",
    }
    for path, content in state_files.items():
        path.write_bytes(content)
    before_state = {path: path.read_bytes() for path in state_files}
    code = main(
        [
            "probe-source-candidate",
            "--url",
            SOURCE_URL,
            "--name",
            "Example",
            "--signal-family",
            "award",
            "--ledger",
            str(ledger),
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["candidates"][0]["name"] == "Example"
    assert {path: path.read_bytes() for path in state_files} == before_state
    assert not (state / "source-candidate-ledger.json").exists()


LAUNCHER = ROOT / "scripts" / "probe-source"


def test_launcher_is_executable_and_wired_without_network() -> None:
    assert LAUNCHER.stat().st_mode & stat.S_IXUSR
    content = LAUNCHER.read_text(encoding="utf-8")
    assert ".venv/bin/python" in content
    assert "python3" in content
    assert "-m autoscout probe-source-candidate" in content
    assert "the launcher itself performs no network" in content.lower()


def test_launcher_uses_fake_interpreter(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    launcher = scripts / "probe-source"
    launcher.write_text(LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")
    launcher.chmod(0o755)
    python = repo / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/usr/bin/env bash\necho "FAKE_PYTHON $@"\n', encoding="utf-8")
    python.chmod(0o755)

    result = subprocess.run([str(launcher), "--url", SOURCE_URL], capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert "FAKE_PYTHON -m autoscout probe-source-candidate --url https://example.test/list" in result.stdout
