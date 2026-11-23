"""Offline acceptance tests for the Issue #336 Source Candidate ledger.

Issue #335 supplies the bounded probe policy, verdict logic, ledger storage,
ordering, duplicate handling, capture behavior, protected destination checks,
and structured result storage.
Issue #336 adds the proposing context, the version-2 ledger with derived counts,
the extended Markdown sidecar, staged pair publication, and deterministic feasibility drafts.
Issue #583 adds version-3 current verdict history while preserving version-1 and version-2 read compatibility.
All tests run offline through the public ``run_source_candidate_probe`` seam or the public CLI with deterministic fake transports only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import autoscout.source_probe as source_probe
from autoscout.__main__ import main
from autoscout.source_probe import DEFAULT_PROPOSING_CONTEXT
from autoscout.source_probe import LEGACY_PROPOSING_CONTEXT
from autoscout.source_probe import SourceCandidateLedgerError
from autoscout.source_probe import run_source_candidate_probe

SOURCE_URL = "https://example.test/list"
SECOND_URL = "https://example.test/second"
THIRD_URL = "https://example.test/third"
DEFER_URL = "https://example.test/defer"
REJECT_URL = "https://example.test/reject"
CONTEXT = "editor weekly review 2026-07-02"


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

    def geturl(self) -> str:
        return self.url

    def read(self, size: int) -> bytes:
        return self.body

    def close(self) -> None:
        pass


class FakeTransport:
    def __init__(self, plans: dict[str, list[Any]]) -> None:
        self.plans = {url: list(responses) for url, responses in plans.items()}
        self.calls: list[tuple[str, str]] = []

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
        return value


class NoNetwork:
    def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("network access was attempted")


def _accepted_plan(url: str = SOURCE_URL) -> dict[str, list[FakeResponse]]:
    return {url: [FakeResponse(200, url=url), FakeResponse(200, _page(), url)]}


def _deferred_plan(url: str = DEFER_URL) -> dict[str, list[FakeResponse]]:
    return {url: [FakeResponse(200, url=url), FakeResponse(200, _page(entries=0, javascript=True), url)]}


def _ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "ledger" / "source-candidates.json"


def _draft_dir(tmp_path: Path) -> Path:
    return tmp_path / "feasibility-drafts"


def _entry(url: str, *, outcome: str = "defer", proposing_context: str | None = CONTEXT) -> dict[str, Any]:
    entry = {
        "normalized_url": source_probe.normalized_url(url),
        "probe_date": "2026-07-02",
        "url": url,
        "name": "Example",
        "signal_family": "award",
        "outcome": outcome,
        "reason_code": "js_only" if outcome == "defer" else "ok",
        "reason": "reason text",
        "http_evidence": {
            "head": {"status": 200, "resolved_url": url, "error": ""},
            "get": {"status": 200, "resolved_url": url, "body_bytes": 100, "truncated": False},
        },
        "head_status": 200,
        "get_status": 200,
        "resolved_url": url,
        "detected_heading": "Current list",
        "bounded_entry_count": 0,
        "samples": [],
    }
    if proposing_context is not None:
        entry["proposing_context"] = proposing_context
    return entry


def _v2_ledger(entries: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"accept": 0, "defer": 0, "reject": 0}
    for entry in entries:
        counts[entry["outcome"]] += 1
    return {
        "version": 2,
        "accept_count": counts["accept"],
        "defer_count": counts["defer"],
        "reject_count": counts["reject"],
        "total_entry_count": len(entries),
        "entries": entries,
    }


def _split_cells(line: str) -> list[str]:
    """Split one table row on unescaped pipes, keeping escapes intact."""
    cells: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(line):
        character = line[index]
        if character == "\\" and index + 1 < len(line):
            current.append(character)
            current.append(line[index + 1])
            index += 2
            continue
        if character == "|":
            cells.append("".join(current))
            current = []
            index += 1
            continue
        current.append(character)
        index += 1
    cells.append("".join(current))
    return cells


def _row_cells(line: str) -> list[str]:
    """Drop the leading and trailing pipe delimiters of one table row."""
    return _split_cells(line)[1:-1]


def _unescape_cell(value: str) -> str:
    """Decode one escaped cell back to the exact canonical value.

    Mirrors the deterministic rendering contract: a literal backslash is
    stored as two backslashes, a literal pipe as backslash-pipe, a newline
    as the two characters backslash-n, and a carriage return as the two
    characters backslash-r."""
    result: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\" and index + 1 < len(value):
            following = value[index + 1]
            if following == "\\":
                result.append("\\")
                index += 2
                continue
            if following == "|":
                result.append("|")
                index += 2
                continue
            if following == "n":
                result.append("\n")
                index += 2
                continue
            if following == "r":
                result.append("\r")
                index += 2
                continue
        result.append(character)
        index += 1
    return "".join(result)


def _markdown_rows(markdown: str) -> list[dict[str, str]]:
    """Parse ledger rows into decoded cells, never a lossy whitespace split.

    Each rendered row is ``| {cell} |`` with exactly one padding space per
    side, so one surrounding space is removed per cell before decoding.
    Every remaining character, including leading and trailing spaces, is
    preserved by the unescape step."""
    lines = markdown.splitlines()
    header_index = next(index for index, line in enumerate(lines) if line.startswith("| URL |"))
    header = [cell.strip() for cell in _row_cells(lines[header_index])]
    rows: list[dict[str, str]] = []
    for line in lines[header_index + 2 :]:
        if not line.strip().startswith("|"):
            continue
        cells: list[str] = []
        for cell in _row_cells(line):
            if cell.startswith(" ") and cell.endswith(" "):
                cell = cell[1:-1]
            cells.append(_unescape_cell(cell))
        rows.append(dict(zip(header, cells, strict=True)))
    return rows


def _write_ledger_file(path: Path, payload: Any) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps(payload, indent=2).encode("utf-8") + b"\n"
    path.write_bytes(original)
    return original


@pytest.fixture(autouse=True)
def _hermetic_default_draft_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the API default draft directory out of the repository.

    The default ``data/source-feasibility-drafts`` path resolves against
    the current directory, so every probe that omits ``feasibility_dir``
    would write into the repository during tests. Chdir to the per-test
    temporary directory to keep every test hermetic."""
    monkeypatch.chdir(tmp_path)


# ---------------------------------------------------------------------------
# Proposing context
# ---------------------------------------------------------------------------


def test_accept_records_metadata_and_proposing_context(tmp_path: Path) -> None:
    transport = FakeTransport(_accepted_plan())
    report, code = run_source_candidate_probe(
        [SOURCE_URL],
        names=["Example list"],
        signal_families=["media/curated list"],
        proposing_contexts=[CONTEXT],
        ledger_path=_ledger_path(tmp_path),
        opener=transport,
    )

    assert code == 0
    assert report["candidates"][0]["proposing_context"] == CONTEXT
    ledger = json.loads(_ledger_path(tmp_path).read_text(encoding="utf-8"))
    assert ledger["version"] == 3
    assert ledger["entries"][0]["proposing_context"] == CONTEXT
    assert ledger["entries"][0]["url"] == SOURCE_URL


def test_defer_and_reject_record_metadata_and_context(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    report, code = run_source_candidate_probe(
        [DEFER_URL],
        proposing_contexts=["defer context"],
        ledger_path=ledger,
        opener=FakeTransport(_deferred_plan()),
    )
    assert code == 3
    assert report["candidates"][0]["outcome"] == "defer"
    assert report["candidates"][0]["proposing_context"] == "defer context"

    report, code = run_source_candidate_probe(
        [REJECT_URL],
        proposing_contexts=["reject context"],
        ledger_path=ledger,
        opener=FakeTransport({REJECT_URL: [FakeResponse(403, url=REJECT_URL)]}),
    )
    assert code == 1
    assert report["candidates"][0]["outcome"] == "reject"
    assert report["candidates"][0]["proposing_context"] == "reject context"

    data = json.loads(ledger.read_text(encoding="utf-8"))
    by_url = {entry["url"]: entry for entry in data["entries"]}
    assert by_url[DEFER_URL]["proposing_context"] == "defer context"
    assert by_url[REJECT_URL]["proposing_context"] == "reject context"


def test_aligned_contexts_apply_per_url(tmp_path: Path) -> None:
    transport = FakeTransport(
        {**_accepted_plan(), SECOND_URL: [FakeResponse(200, url=SECOND_URL), FakeResponse(200, _page(), SECOND_URL)]}
    )
    report, code = run_source_candidate_probe(
        [SOURCE_URL, SECOND_URL],
        proposing_contexts=[CONTEXT, "market scan July"],
        ledger_path=_ledger_path(tmp_path),
        opener=transport,
    )

    assert code == 0
    contexts = {candidate["url"]: candidate["proposing_context"] for candidate in report["candidates"]}
    assert contexts == {SOURCE_URL: CONTEXT, SECOND_URL: "market scan July"}


def test_visible_title_selector_reaches_feasibility_evaluation(tmp_path: Path) -> None:
    html = (
        b"<html><head><title>Stale wrong title</title></head><body>"
        b'<div class="pn-editoria-titulo">PublishNews visible title</div>'
        b'<div class="pn-row">1. Book One</div></body></html>'
    )
    transport = FakeTransport({SOURCE_URL: [FakeResponse(200, url=SOURCE_URL), FakeResponse(200, html, SOURCE_URL)]})
    report, code = run_source_candidate_probe(
        [SOURCE_URL],
        visible_title_selectors=[".pn-editoria-titulo"],
        ledger_path=_ledger_path(tmp_path),
        opener=transport,
    )

    result = report["candidates"][0]
    assert code == 1
    assert result["reason_code"] == "unlinked_entries"
    assert result["detected_heading"] == "PublishNews visible title"


def test_probe_cli_forwards_visible_title_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    html = (
        b"<html><head><title>Stale wrong title</title></head><body>"
        b'<div class="pn-editoria-titulo">PublishNews visible title</div>'
        b'<div class="pn-row">1. Book One</div></body></html>'
    )
    transport = FakeTransport({SOURCE_URL: [FakeResponse(200, url=SOURCE_URL), FakeResponse(200, html, SOURCE_URL)]})
    monkeypatch.setattr(source_probe, "urlopen", transport)
    code = main(
        [
            "probe-source-candidate",
            "--url",
            SOURCE_URL,
            "--visible-title-selector",
            ".pn-editoria-titulo",
            "--ledger",
            str(_ledger_path(tmp_path)),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert code == 1
    assert report["candidates"][0]["reason_code"] == "unlinked_entries"
    assert report["candidates"][0]["detected_heading"] == "PublishNews visible title"


def test_visible_title_selectors_align_per_url_and_default_to_absent(tmp_path: Path) -> None:
    html = (
        b"<html><head><title>Stale wrong title</title></head><body>"
        b'<div class="pn-editoria-titulo">PublishNews visible title</div>'
        b'<div class="pn-row">1. Book One</div></body></html>'
    )
    second_url = SECOND_URL
    transport = FakeTransport(
        {
            SOURCE_URL: [FakeResponse(200, url=SOURCE_URL), FakeResponse(200, html, SOURCE_URL)],
            second_url: [FakeResponse(200, url=second_url), FakeResponse(200, html, second_url)],
        }
    )
    report, code = run_source_candidate_probe(
        [SOURCE_URL, second_url],
        visible_title_selectors=[".pn-editoria-titulo"],
        ledger_path=_ledger_path(tmp_path),
        opener=transport,
    )

    by_url = {candidate["url"]: candidate for candidate in report["candidates"]}
    assert code == 1
    assert by_url[SOURCE_URL]["reason_code"] == "unlinked_entries"
    assert by_url[second_url]["reason_code"] == "missing_heading"


@pytest.mark.parametrize("selector", ["", "   ", "div.foo", ".a .b", ".a > span"])
def test_visible_title_selector_is_validated_before_network(tmp_path: Path, selector: str) -> None:
    with pytest.raises(ValueError, match="visible title selector"):
        run_source_candidate_probe(
            [SOURCE_URL],
            visible_title_selectors=[selector],
            ledger_path=_ledger_path(tmp_path),
            opener=NoNetwork(),
        )
    assert not _ledger_path(tmp_path).exists()


def test_aligned_candidate_fields_apply_per_url(tmp_path: Path) -> None:
    transport = FakeTransport(
        {**_accepted_plan(), SECOND_URL: [FakeResponse(200, url=SECOND_URL), FakeResponse(200, _page(), SECOND_URL)]}
    )
    report, code = run_source_candidate_probe(
        [SOURCE_URL, SECOND_URL],
        names=["First list", "Second list"],
        signal_families=["media/curated list", "award"],
        proposing_contexts=[CONTEXT, "second context"],
        ledger_path=_ledger_path(tmp_path),
        opener=transport,
    )

    assert code == 0
    by_url = {candidate["url"]: candidate for candidate in report["candidates"]}
    assert by_url[SOURCE_URL]["name"] == "First list"
    assert by_url[SOURCE_URL]["signal_family"] == "media/curated list"
    assert by_url[SOURCE_URL]["proposing_context"] == CONTEXT
    assert by_url[SECOND_URL]["name"] == "Second list"
    assert by_url[SECOND_URL]["signal_family"] == "award"
    assert by_url[SECOND_URL]["proposing_context"] == "second context"


def test_too_many_contexts_rejected_before_network(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)

    with pytest.raises(ValueError, match="proposing-context"):
        run_source_candidate_probe(
            [SOURCE_URL],
            proposing_contexts=[CONTEXT, "extra context"],
            ledger_path=ledger,
            opener=NoNetwork(),
        )

    assert not ledger.exists()
    assert not ledger.with_suffix(".md").exists()


def test_missing_context_gets_deterministic_default(tmp_path: Path) -> None:
    transport = FakeTransport(_accepted_plan())
    report, code = run_source_candidate_probe([SOURCE_URL], ledger_path=_ledger_path(tmp_path), opener=transport)

    assert code == 0
    assert report["candidates"][0]["proposing_context"] == DEFAULT_PROPOSING_CONTEXT
    ledger = json.loads(_ledger_path(tmp_path).read_text(encoding="utf-8"))
    assert ledger["entries"][0]["proposing_context"] == DEFAULT_PROPOSING_CONTEXT
    assert DEFAULT_PROPOSING_CONTEXT != LEGACY_PROPOSING_CONTEXT


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n", "\r\n"])
def test_supplied_blank_context_is_rejected_before_network(tmp_path: Path, blank: str) -> None:
    ledger = _ledger_path(tmp_path)

    with pytest.raises(ValueError, match="proposing context"):
        run_source_candidate_probe([SOURCE_URL], proposing_contexts=[blank], ledger_path=ledger, opener=NoNetwork())

    assert not ledger.exists()
    assert not ledger.with_suffix(".md").exists()


def test_supplied_blank_context_is_rejected_in_batch_before_network(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)

    with pytest.raises(ValueError, match="proposing context"):
        run_source_candidate_probe(
            [SOURCE_URL, SECOND_URL],
            proposing_contexts=[CONTEXT, "   "],
            ledger_path=ledger,
            opener=NoNetwork(),
        )

    assert not ledger.exists()
    assert not ledger.with_suffix(".md").exists()


def test_supplied_blank_context_is_rejected_before_sidecar_repair_write(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    original = _write_ledger_file(ledger, _v2_ledger([_entry(SECOND_URL)]))
    markdown_path = ledger.with_suffix(".md")
    markdown_path.write_text("STALE SIDECAR\n", encoding="utf-8")

    with pytest.raises(ValueError, match="proposing context"):
        run_source_candidate_probe([SOURCE_URL], proposing_contexts=["   "], ledger_path=ledger, opener=NoNetwork())

    assert ledger.read_bytes() == original
    assert markdown_path.read_text(encoding="utf-8") == "STALE SIDECAR\n"


def test_supplied_context_is_preserved_exactly(tmp_path: Path) -> None:
    exact = "  editor   proposed  during live review  "
    transport = FakeTransport(_accepted_plan())
    report, code = run_source_candidate_probe(
        [SOURCE_URL], proposing_contexts=[exact], ledger_path=_ledger_path(tmp_path), opener=transport
    )

    assert code == 0
    assert report["candidates"][0]["proposing_context"] == exact
    ledger = json.loads(_ledger_path(tmp_path).read_text(encoding="utf-8"))
    assert ledger["entries"][0]["proposing_context"] == exact


def test_proposing_context_stays_exact_in_draft_and_markdown(tmp_path: Path) -> None:
    exact = "  editor   proposed during live review  "
    ledger = _ledger_path(tmp_path)
    drafts = _draft_dir(tmp_path)
    transport = FakeTransport(_accepted_plan())
    report, code = run_source_candidate_probe(
        [SOURCE_URL], proposing_contexts=[exact], ledger_path=ledger, feasibility_dir=drafts, opener=transport
    )

    assert code == 0
    assert report["candidates"][0]["proposing_context"] == exact
    data = json.loads(ledger.read_text(encoding="utf-8"))
    assert data["entries"][0]["proposing_context"] == exact
    markdown = ledger.with_suffix(".md").read_text(encoding="utf-8")
    assert f"| {exact} |" in markdown
    draft = next(drafts.glob("*.md")).read_text(encoding="utf-8")
    assert f"- Proposing context: {exact}" in draft


def test_new_entries_preserve_original_values_and_context(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    transport = FakeTransport(
        {**_accepted_plan(), SECOND_URL: [FakeResponse(200, url=SECOND_URL), FakeResponse(200, _page(), SECOND_URL)]}
    )
    report, code = run_source_candidate_probe(
        [SOURCE_URL, SECOND_URL],
        proposing_contexts=[CONTEXT, "second context"],
        ledger_path=ledger,
        opener=transport,
    )
    assert code == 0
    first = json.loads(ledger.read_text(encoding="utf-8"))
    first_by_identity = {entry["normalized_url"]: entry for entry in first["entries"]}

    report, code = run_source_candidate_probe(
        [THIRD_URL],
        proposing_contexts=["third context"],
        ledger_path=ledger,
        opener=FakeTransport(_accepted_plan(THIRD_URL)),
    )
    assert code == 0
    second = json.loads(ledger.read_text(encoding="utf-8"))
    second_by_identity = {entry["normalized_url"]: entry for entry in second["entries"]}
    assert second["version"] == 3
    assert (
        second_by_identity[source_probe.normalized_url(SOURCE_URL)]
        == first_by_identity[source_probe.normalized_url(SOURCE_URL)]
    )
    assert (
        second_by_identity[source_probe.normalized_url(SECOND_URL)]
        == first_by_identity[source_probe.normalized_url(SECOND_URL)]
    )
    assert second_by_identity[source_probe.normalized_url(SOURCE_URL)]["proposing_context"] == CONTEXT
    assert second_by_identity[source_probe.normalized_url(SECOND_URL)]["proposing_context"] == "second context"


def test_duplicate_uses_no_network_and_retains_original_context(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    transport = FakeTransport(_accepted_plan())
    report, code = run_source_candidate_probe(
        [SOURCE_URL], proposing_contexts=[CONTEXT], ledger_path=ledger, opener=transport
    )
    assert code == 0

    report, code = run_source_candidate_probe(
        [SOURCE_URL],
        proposing_contexts=["different context"],
        ledger_path=ledger,
        opener=NoNetwork(),
    )
    assert code == 0
    result = report["candidates"][0]
    assert result["reason_code"] == "already_evaluated"
    assert result["proposing_context"] == CONTEXT


def test_api_default_feasibility_dir_writes_draft(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    transport = FakeTransport(_accepted_plan())
    report, code = run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, opener=transport)

    assert code == 0
    assert report["candidates"][0]["draft_status"] == "created"
    default_dir = tmp_path / "data" / "source-feasibility-drafts"
    assert list(default_dir.glob("*.md"))


# ---------------------------------------------------------------------------
# Version-2 ledger
# ---------------------------------------------------------------------------


def test_version3_counts_are_derived_from_current_entries(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    run_source_candidate_probe([DEFER_URL], ledger_path=ledger, opener=FakeTransport(_deferred_plan()))
    run_source_candidate_probe(
        [REJECT_URL], ledger_path=ledger, opener=FakeTransport({REJECT_URL: [FakeResponse(403, url=REJECT_URL)]})
    )
    run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, opener=FakeTransport(_accepted_plan()))

    data = json.loads(ledger.read_text(encoding="utf-8"))
    assert data["version"] == 3
    assert data["accept_count"] == 1
    assert data["defer_count"] == 1
    assert data["reject_count"] == 1
    assert data["total_entry_count"] == 3
    assert data["accept_count"] + data["defer_count"] + data["reject_count"] == data["total_entry_count"]
    outcomes = sorted(entry["outcome"] for entry in data["entries"])
    assert outcomes == ["accept", "defer", "reject"]


def test_markdown_shows_counts_and_matches_json_entries(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    run_source_candidate_probe([DEFER_URL], ledger_path=ledger, opener=FakeTransport(_deferred_plan()))
    run_source_candidate_probe(
        [REJECT_URL], ledger_path=ledger, opener=FakeTransport({REJECT_URL: [FakeResponse(403, url=REJECT_URL)]})
    )
    run_source_candidate_probe(
        [SOURCE_URL], proposing_contexts=[CONTEXT], ledger_path=ledger, opener=FakeTransport(_accepted_plan())
    )

    data = json.loads(ledger.read_text(encoding="utf-8"))
    markdown = ledger.with_suffix(".md").read_text(encoding="utf-8")
    assert "Total entry count: 3" in markdown
    assert "Accept count: 1" in markdown
    assert "Defer count: 1" in markdown
    assert "Reject count: 1" in markdown

    rows = _markdown_rows(markdown)
    assert len(rows) == 3
    json_by_url = {entry["url"]: entry for entry in data["entries"]}
    assert {row["URL"] for row in rows} == set(json_by_url)
    for row in rows:
        entry = json_by_url[row["URL"]]
        assert row["Probe date"] == entry["probe_date"]
        assert row["Outcome"] == entry["outcome"]
        assert row["Reason code"] == entry["reason_code"]
        assert row["Reason"] == entry["reason"]
        assert row["Proposing context"] == entry["proposing_context"]


def test_markdown_rendering_is_byte_identical_for_unchanged_entries(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    transport = FakeTransport(
        {**_accepted_plan(), SECOND_URL: [FakeResponse(200, url=SECOND_URL), FakeResponse(200, _page(), SECOND_URL)]}
    )
    run_source_candidate_probe([SOURCE_URL, SECOND_URL], ledger_path=ledger, opener=transport)
    markdown_first = ledger.with_suffix(".md").read_text(encoding="utf-8")
    data_first = json.loads(ledger.read_text(encoding="utf-8"))

    run_source_candidate_probe([THIRD_URL], ledger_path=ledger, opener=FakeTransport(_accepted_plan(THIRD_URL)))
    markdown_second = ledger.with_suffix(".md").read_text(encoding="utf-8")
    data_second = json.loads(ledger.read_text(encoding="utf-8"))

    assert data_first["entries"] == data_second["entries"][:2]
    assert _markdown_rows(markdown_first) == _markdown_rows(markdown_second)[:2]
    assert [row["URL"] for row in _markdown_rows(markdown_second)] == [
        SOURCE_URL,
        SECOND_URL,
        THIRD_URL,
    ]


def test_markdown_preserves_exact_values_with_escapes(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    pipe_url = "https://example.test/odd|path"
    reason = "first line\nsecond | pipe line with \\ backslash"
    context = "  context with\nnewline, | pipe, and trailing spaces  "
    entry = _entry(pipe_url)
    entry["reason"] = reason
    entry["proposing_context"] = context
    payload = _v2_ledger([entry])
    original = _write_ledger_file(ledger, payload)

    report, code = run_source_candidate_probe([pipe_url], ledger_path=ledger, opener=NoNetwork())

    assert code == 3
    assert report["candidates"][0]["reason_code"] == "already_evaluated"
    assert ledger.read_bytes() == original
    markdown = ledger.with_suffix(".md").read_text(encoding="utf-8")
    assert "\\n" in markdown
    rows = _markdown_rows(markdown)
    assert len(rows) == 1
    json_entry = json.loads(ledger.read_text(encoding="utf-8"))["entries"][0]
    row = rows[0]
    assert row["URL"] == json_entry["url"]
    assert row["Probe date"] == json_entry["probe_date"]
    assert row["Outcome"] == json_entry["outcome"]
    assert row["Reason code"] == json_entry["reason_code"]
    assert row["Reason"] == json_entry["reason"]
    assert row["Proposing context"] == json_entry["proposing_context"]


def test_multiline_proposing_context_is_preserved_in_markdown(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    context = "proposed in\nweekly review\nwith | pipe and \\ backslash"
    transport = FakeTransport(_accepted_plan())
    report, code = run_source_candidate_probe(
        [SOURCE_URL], proposing_contexts=[context], ledger_path=ledger, opener=transport
    )

    assert code == 0
    assert report["candidates"][0]["proposing_context"] == context
    data = json.loads(ledger.read_text(encoding="utf-8"))
    assert data["entries"][0]["proposing_context"] == context
    rows = _markdown_rows(ledger.with_suffix(".md").read_text(encoding="utf-8"))
    assert rows[0]["Proposing context"] == context


@pytest.mark.parametrize("context", ["before\rafter", "before\r\nafter"])
def test_carriage_return_context_round_trips_losslessly(tmp_path: Path, context: str) -> None:
    ledger = _ledger_path(tmp_path)
    transport = FakeTransport(_accepted_plan())
    report, code = run_source_candidate_probe(
        [SOURCE_URL], proposing_contexts=[context], ledger_path=ledger, opener=transport
    )

    assert code == 0
    assert report["candidates"][0]["proposing_context"] == context
    data = json.loads(ledger.read_text(encoding="utf-8"))
    assert data["entries"][0]["proposing_context"] == context
    markdown_path = ledger.with_suffix(".md")
    assert b"\r" not in markdown_path.read_bytes()
    rows = _markdown_rows(markdown_path.read_text(encoding="utf-8"))
    assert len(rows) == 1
    assert rows[0]["Proposing context"] == context


@pytest.mark.parametrize("context", ["before\rafter", "before\r\nafter"])
def test_carriage_return_context_duplicate_retry_keeps_sidecar_bytes(tmp_path: Path, context: str) -> None:
    ledger = _ledger_path(tmp_path)
    transport = FakeTransport(_accepted_plan())
    report, code = run_source_candidate_probe(
        [SOURCE_URL], proposing_contexts=[context], ledger_path=ledger, opener=transport
    )
    assert code == 0
    json_before = ledger.read_bytes()
    markdown_path = ledger.with_suffix(".md")
    markdown_before = markdown_path.read_bytes()
    stat_before = markdown_path.stat()

    report, code = run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, opener=NoNetwork())

    assert code == 0
    assert report["candidates"][0]["reason_code"] == "already_evaluated"
    assert ledger.read_bytes() == json_before
    assert markdown_path.read_bytes() == markdown_before
    stat_after = markdown_path.stat()
    assert (stat_after.st_mtime_ns, stat_after.st_size) == (stat_before.st_mtime_ns, stat_before.st_size)


def test_carriage_return_reason_and_context_round_trip_from_seeded_ledger(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    entry = _entry(SECOND_URL)
    entry["reason"] = "first line\rsecond line"
    entry["proposing_context"] = "context\r\nwith both"
    original = _write_ledger_file(ledger, _v2_ledger([entry]))

    report, code = run_source_candidate_probe([SECOND_URL], ledger_path=ledger, opener=NoNetwork())

    assert code == 3
    assert report["candidates"][0]["reason_code"] == "already_evaluated"
    markdown_path = ledger.with_suffix(".md")
    assert b"\r" not in markdown_path.read_bytes()
    rows = _markdown_rows(markdown_path.read_text(encoding="utf-8"))
    json_entry = json.loads(ledger.read_text(encoding="utf-8"))["entries"][0]
    assert rows[0]["Reason"] == json_entry["reason"]
    assert rows[0]["Proposing context"] == json_entry["proposing_context"]

    markdown_before = markdown_path.read_bytes()
    stat_before = markdown_path.stat()
    run_source_candidate_probe([SECOND_URL], ledger_path=ledger, opener=NoNetwork())
    assert markdown_path.read_bytes() == markdown_before
    assert markdown_path.stat().st_mtime_ns == stat_before.st_mtime_ns
    assert ledger.read_bytes() == original


def test_malformed_ledger_encoding_is_a_controlled_error_before_network(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    original = b'{"version": 1, "entries": [], "broken": "\xff\xfe"}'
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(original)

    with pytest.raises(SourceCandidateLedgerError, match="cannot read ledger"):
        run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, opener=NoNetwork())

    assert ledger.read_bytes() == original
    assert not ledger.with_suffix(".md").exists()


def test_valid_version1_ledger_is_read_and_upgraded_on_write(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    v1_entry = {
        "normalized_url": source_probe.normalized_url(SECOND_URL),
        "probe_date": "2026-06-25",
        "url": SECOND_URL,
        "name": "Legacy list",
        "signal_family": "award",
        "outcome": "defer",
        "reason_code": "js_only",
        "reason": "page renders book data with JavaScript only; re-probe later",
        "http_evidence": {
            "head": {"status": 200, "resolved_url": SECOND_URL, "error": ""},
            "get": {"status": 200, "resolved_url": SECOND_URL, "body_bytes": 120, "truncated": False},
        },
        "head_status": 200,
        "get_status": 200,
        "resolved_url": SECOND_URL,
        "detected_heading": "Current list",
        "bounded_entry_count": 0,
        "samples": [],
    }
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"version": 1, "entries": [v1_entry]}), encoding="utf-8")

    transport = FakeTransport(_accepted_plan())
    report, code = run_source_candidate_probe(
        [SOURCE_URL], proposing_contexts=[CONTEXT], ledger_path=ledger, opener=transport
    )
    assert code == 0

    data = json.loads(ledger.read_text(encoding="utf-8"))
    assert data["version"] == 3
    assert data["defer_count"] == 1
    assert data["accept_count"] == 1
    entries_by_url = {entry["url"]: entry for entry in data["entries"]}
    upgraded = entries_by_url[SECOND_URL]
    assert upgraded["proposing_context"] == LEGACY_PROPOSING_CONTEXT
    for field, value in v1_entry.items():
        assert upgraded[field] == value
    assert entries_by_url[SOURCE_URL]["proposing_context"] == CONTEXT


def test_version1_ledger_json_is_not_rewritten_by_duplicate_read(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    v1_entry = _entry(SECOND_URL, proposing_context=None)
    original = _write_ledger_file(ledger, {"version": 1, "entries": [v1_entry]})

    report, code = run_source_candidate_probe([SECOND_URL], ledger_path=ledger, opener=NoNetwork())

    assert code == 3
    assert report["candidates"][0]["reason_code"] == "already_evaluated"
    assert ledger.read_bytes() == original
    markdown = ledger.with_suffix(".md").read_text(encoding="utf-8")
    assert SECOND_URL in markdown
    assert LEGACY_PROPOSING_CONTEXT in markdown


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 3, "entries": []},
        {"version": "1", "entries": []},
        {
            "version": 2,
            "accept_count": 0,
            "defer_count": 0,
            "reject_count": 0,
            "total_entry_count": 0,
            "entries": [],
            "extra": True,
        },
        {"version": 2, "accept_count": 1, "defer_count": 0, "reject_count": 0, "total_entry_count": 0, "entries": []},
        {
            "version": 2,
            "accept_count": True,
            "defer_count": 0,
            "reject_count": 0,
            "total_entry_count": 0,
            "entries": [],
        },
        {"version": 2, "accept_count": 0, "defer_count": 0, "reject_count": 0, "total_entry_count": 0, "entries": [{}]},
        {
            "version": 2,
            "accept_count": 0,
            "defer_count": 0,
            "reject_count": 0,
            "total_entry_count": 2,
            "entries": [_entry(SECOND_URL)],
        },
    ],
)
def test_malformed_and_future_schemas_rejected_before_network(tmp_path: Path, payload: Any) -> None:
    ledger = _ledger_path(tmp_path)
    original = _write_ledger_file(ledger, payload)

    with pytest.raises(SourceCandidateLedgerError):
        run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, opener=NoNetwork())

    assert ledger.read_bytes() == original
    assert not ledger.with_suffix(".md").exists()


def test_valid_version2_ledger_is_read_without_json_rewrite(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    payload = _v2_ledger([_entry(SECOND_URL)])
    original = _write_ledger_file(ledger, payload)

    report, code = run_source_candidate_probe([SECOND_URL], ledger_path=ledger, opener=NoNetwork())

    assert code == 3
    assert report["candidates"][0]["reason_code"] == "already_evaluated"
    assert report["candidates"][0]["proposing_context"] == CONTEXT
    assert ledger.read_bytes() == original
    markdown = ledger.with_suffix(".md").read_text(encoding="utf-8")
    assert "Total entry count: 1" in markdown
    assert SECOND_URL in markdown
    assert CONTEXT in markdown


def test_duplicate_run_repairs_tampered_markdown_without_network(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    transport = FakeTransport(_accepted_plan())
    report, code = run_source_candidate_probe(
        [SOURCE_URL], proposing_contexts=[CONTEXT], ledger_path=ledger, opener=transport
    )
    assert code == 0
    json_before = ledger.read_bytes()
    markdown_path = ledger.with_suffix(".md")
    markdown_path.write_text("STALE MARKDOWN\n", encoding="utf-8")

    report, code = run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, opener=NoNetwork())

    assert code == 0
    assert report["candidates"][0]["reason_code"] == "already_evaluated"
    assert ledger.read_bytes() == json_before
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "STALE MARKDOWN" not in markdown
    assert "Total entry count: 1" in markdown
    assert "Accept count: 1" in markdown
    rows = _markdown_rows(markdown)
    assert len(rows) == 1
    data = json.loads(ledger.read_text(encoding="utf-8"))
    assert rows[0]["URL"] == data["entries"][0]["url"]
    assert rows[0]["Proposing context"] == data["entries"][0]["proposing_context"]


def test_duplicate_run_repairs_sidecar_with_wrong_counts(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    payload = _v2_ledger([_entry(SECOND_URL, outcome="accept")])
    _write_ledger_file(ledger, payload)
    markdown_path = ledger.with_suffix(".md")
    markdown_path.write_text("# Source Candidate Ledger\n\nTotal entry count: 99\n", encoding="utf-8")

    run_source_candidate_probe([SECOND_URL], ledger_path=ledger, opener=NoNetwork())

    markdown = markdown_path.read_text(encoding="utf-8")
    assert "Total entry count: 1" in markdown
    assert "Accept count: 1" in markdown
    assert json.loads(ledger.read_text(encoding="utf-8")) == payload


def test_failed_sidecar_repair_keeps_previous_sidecar_and_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = _ledger_path(tmp_path)
    transport = FakeTransport(_accepted_plan())
    run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, opener=transport)
    json_before = ledger.read_bytes()
    markdown_path = ledger.with_suffix(".md")
    markdown_path.write_text("STALE MARKDOWN\n", encoding="utf-8")

    real_replace = source_probe._staged_replace
    markdown_destination = markdown_path.resolve()

    def failing_replace(source: Path, destination: Path) -> None:
        if destination == markdown_destination:
            raise OSError("injected sidecar repair failure")
        return real_replace(source, destination)

    monkeypatch.setattr(source_probe, "_staged_replace", failing_replace)
    with pytest.raises(SourceCandidateLedgerError, match="sidecar"):
        run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, opener=NoNetwork())

    assert markdown_path.read_text(encoding="utf-8") == "STALE MARKDOWN\n"
    assert ledger.read_bytes() == json_before
    assert list(ledger.parent.glob("*.stage")) == []


def test_version2_entry_without_context_is_rejected_before_network(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    original = _write_ledger_file(ledger, _v2_ledger([_entry(SECOND_URL, proposing_context=None)]))

    with pytest.raises(SourceCandidateLedgerError, match="proposing_context"):
        run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, opener=NoNetwork())

    assert ledger.read_bytes() == original


@pytest.mark.parametrize("bad_context", [123, True, None, [], {"nested": 1}])
def test_version2_entry_with_non_string_context_is_rejected_before_network(tmp_path: Path, bad_context: Any) -> None:
    ledger = _ledger_path(tmp_path)
    entry = _entry(SECOND_URL)
    entry["proposing_context"] = bad_context
    original = _write_ledger_file(ledger, _v2_ledger([entry]))

    with pytest.raises(SourceCandidateLedgerError, match="proposing_context"):
        run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, opener=NoNetwork())

    assert ledger.read_bytes() == original
    assert not ledger.with_suffix(".md").exists()


def test_version1_entry_with_context_is_rejected_as_ambiguous_before_network(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    original = _write_ledger_file(ledger, {"version": 1, "entries": [_entry(SECOND_URL)]})

    with pytest.raises(SourceCandidateLedgerError, match="unknown fields"):
        run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, opener=NoNetwork())

    assert ledger.read_bytes() == original


# ---------------------------------------------------------------------------
# Staged pair publication
# ---------------------------------------------------------------------------


def test_injected_pair_publication_failure_rolls_back_both_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _ledger_path(tmp_path)
    transport = FakeTransport(_accepted_plan())
    report, code = run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, opener=transport)
    assert code == 0
    json_before = ledger.read_bytes()
    markdown_before = ledger.with_suffix(".md").read_bytes()

    real_replace = source_probe._staged_replace
    markdown_destination = ledger.with_suffix(".md").resolve()

    def failing_replace(source: Path, destination: Path) -> None:
        if destination == markdown_destination:
            raise OSError("injected markdown publish failure")
        return real_replace(source, destination)

    monkeypatch.setattr(source_probe, "_staged_replace", failing_replace)
    with pytest.raises(SourceCandidateLedgerError, match="publish"):
        run_source_candidate_probe(
            [SECOND_URL],
            ledger_path=ledger,
            opener=FakeTransport(
                {SECOND_URL: [FakeResponse(200, url=SECOND_URL), FakeResponse(200, _page(), SECOND_URL)]}
            ),
        )

    assert ledger.read_bytes() == json_before
    assert ledger.with_suffix(".md").read_bytes() == markdown_before
    assert list(ledger.parent.glob("*.stage")) == []
    assert list(ledger.parent.glob("*.backup")) == []


def test_replace_then_raise_failure_rolls_back_both_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = _ledger_path(tmp_path)
    transport = FakeTransport(_accepted_plan())
    report, code = run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, opener=transport)
    assert code == 0
    json_before = ledger.read_bytes()
    markdown_before = ledger.with_suffix(".md").read_bytes()

    real_replace = source_probe._staged_replace
    markdown_destination = ledger.with_suffix(".md").resolve()

    def replace_then_raise(source: Path, destination: Path) -> None:
        if destination == markdown_destination:
            real_replace(source, destination)
            raise OSError("injected replace-then-raise failure")
        return real_replace(source, destination)

    monkeypatch.setattr(source_probe, "_staged_replace", replace_then_raise)
    with pytest.raises(SourceCandidateLedgerError, match="publish"):
        run_source_candidate_probe(
            [SECOND_URL],
            ledger_path=ledger,
            opener=FakeTransport(
                {SECOND_URL: [FakeResponse(200, url=SECOND_URL), FakeResponse(200, _page(), SECOND_URL)]}
            ),
        )

    assert ledger.read_bytes() == json_before
    assert ledger.with_suffix(".md").read_bytes() == markdown_before
    assert list(ledger.parent.glob("*.stage")) == []
    assert list(ledger.parent.glob("*.backup")) == []


def test_publication_failure_surfaces_as_contract_error_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    ledger = _ledger_path(tmp_path)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"version": 1, "entries": []}), encoding="utf-8")
    real_replace = source_probe._staged_replace

    def failing_replace(source: Path, destination: Path) -> None:
        if destination.name.endswith(".md"):
            raise OSError("injected markdown publish failure")
        return real_replace(source, destination)

    monkeypatch.setattr("autoscout.source_probe.urlopen", FakeTransport(_accepted_plan()))
    monkeypatch.setattr(source_probe, "_staged_replace", failing_replace)
    code = main(
        [
            "probe-source-candidate",
            "--url",
            SOURCE_URL,
            "--ledger",
            str(ledger),
            "--feasibility-dir",
            str(tmp_path / "drafts"),
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert code == 2
    assert "error" in report
    assert json.loads(ledger.read_text(encoding="utf-8")) == {"version": 1, "entries": []}
    assert not ledger.with_suffix(".md").exists()


# ---------------------------------------------------------------------------
# Feasibility drafts
# ---------------------------------------------------------------------------


def test_accepted_candidate_gets_feasibility_draft_with_grounded_fields(tmp_path: Path) -> None:
    drafts = _draft_dir(tmp_path)
    transport = FakeTransport(_accepted_plan())
    report, code = run_source_candidate_probe(
        [SOURCE_URL],
        names=["Example list"],
        signal_families=["media/curated list"],
        proposing_contexts=[CONTEXT],
        ledger_path=_ledger_path(tmp_path),
        feasibility_dir=drafts,
        opener=transport,
    )

    assert code == 0
    result = report["candidates"][0]
    assert result["draft_status"] == "created"
    assert result["draft_file"].endswith(".md")
    files = list(drafts.glob("*.md"))
    assert len(files) == 1
    draft = files[0].read_text(encoding="utf-8")
    assert "Draft status: draft" in draft
    assert "needs editor or agent judgment" in draft
    assert "Example list" in draft
    assert f"Candidate URL: {SOURCE_URL}" in draft
    assert f"Resolved URL: {SOURCE_URL}" in draft
    assert "Probe date: " in draft
    assert "media/curated list" in draft
    assert f"Proposing context: {CONTEXT}" in draft
    assert "Verdict: accept" in draft
    assert "Reason code: ok" in draft
    assert "Plain-language reason:" in draft
    assert "Detected heading: Current list" in draft
    assert "Bounded entry count: 2" in draft
    assert "| Book 1 | Jane Author |" in draft
    assert f"{SOURCE_URL}/book-1" in draft
    assert "| Book 2 | Jane Author |" in draft
    assert "## Editorial rationale" in draft
    assert "incomplete" in draft
    assert "No editorial rationale is invented" in draft


def test_draft_contains_no_raw_html_or_copied_prose(tmp_path: Path) -> None:
    drafts = _draft_dir(tmp_path)
    transport = FakeTransport(_accepted_plan())
    report, code = run_source_candidate_probe(
        [SOURCE_URL], ledger_path=_ledger_path(tmp_path), feasibility_dir=drafts, opener=transport
    )

    assert code == 0
    draft = next(drafts.glob("*.md")).read_text(encoding="utf-8")
    assert "<" not in draft
    assert "By:" not in draft
    assert "<script" not in draft


def test_defer_and_reject_never_create_draft_or_capture(tmp_path: Path) -> None:
    drafts = _draft_dir(tmp_path)
    transport = FakeTransport({**_deferred_plan(), REJECT_URL: [FakeResponse(403, url=REJECT_URL)]})
    report, code = run_source_candidate_probe(
        [DEFER_URL, REJECT_URL],
        ledger_path=_ledger_path(tmp_path),
        feasibility_dir=drafts,
        opener=transport,
    )

    assert code == 1
    assert [candidate["outcome"] for candidate in report["candidates"]] == ["defer", "reject"]
    assert all("draft_status" not in candidate for candidate in report["candidates"])
    assert all("draft_file" not in candidate for candidate in report["candidates"])
    assert not drafts.exists()
    assert not (tmp_path / "captures").exists()


def test_existing_draft_is_preserved_byte_for_byte(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    drafts = _draft_dir(tmp_path)
    transport = FakeTransport(_accepted_plan())
    report, code = run_source_candidate_probe(
        [SOURCE_URL], ledger_path=ledger, feasibility_dir=drafts, opener=transport
    )
    assert report["candidates"][0]["draft_status"] == "created"
    draft_file = next(drafts.glob("*.md"))
    before = draft_file.read_bytes()

    report, code = run_source_candidate_probe(
        [SOURCE_URL], ledger_path=ledger, feasibility_dir=drafts, opener=NoNetwork()
    )
    assert code == 0
    assert report["candidates"][0]["draft_status"] == "already_present"
    assert draft_file.read_bytes() == before
    assert list(drafts.glob("*.md")) == [draft_file]


def test_missing_accepted_draft_is_regenerated_without_network(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    transport = FakeTransport(_accepted_plan())
    report, code = run_source_candidate_probe(
        [SOURCE_URL], proposing_contexts=[CONTEXT], ledger_path=ledger, opener=transport
    )
    assert code == 0
    assert not _draft_dir(tmp_path).exists()

    report, code = run_source_candidate_probe(
        [SOURCE_URL], ledger_path=ledger, feasibility_dir=_draft_dir(tmp_path), opener=NoNetwork()
    )
    assert code == 0
    result = report["candidates"][0]
    assert result["reason_code"] == "already_evaluated"
    assert result["draft_status"] == "regenerated"
    draft = next(_draft_dir(tmp_path).glob("*.md")).read_text(encoding="utf-8")
    assert f"Proposing context: {CONTEXT}" in draft
    assert "Verdict: accept" in draft


def test_protected_and_symlinked_feasibility_destinations_rejected_before_network(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    protected = tmp_path / "runs" / "current-weekly-pilot-russian"
    protected.mkdir(parents=True)
    protected_file = protected / "candidate-history.json"
    protected_file.write_bytes(b"protected weekly state\n")
    before = protected_file.read_bytes()

    with pytest.raises(SourceCandidateLedgerError):
        run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, feasibility_dir=protected, opener=NoNetwork())
    assert protected_file.read_bytes() == before

    alias = tmp_path / "alias"
    alias.symlink_to(protected, target_is_directory=True)
    with pytest.raises(SourceCandidateLedgerError):
        run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, feasibility_dir=alias, opener=NoNetwork())
    assert protected_file.read_bytes() == before


def test_symlinked_draft_target_is_rejected_before_network(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    drafts = _draft_dir(tmp_path)
    drafts.mkdir(parents=True)
    target = tmp_path / "editorial-profile.json"
    target.write_bytes(b"protected state\n")
    draft_path = source_probe._draft_path(drafts, SOURCE_URL)
    draft_path.symlink_to(target)

    with pytest.raises(SourceCandidateLedgerError, match="symlink"):
        run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, feasibility_dir=drafts, opener=NoNetwork())
    assert target.read_bytes() == b"protected state\n"


def test_hardlinked_draft_target_is_rejected_before_network(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    drafts = _draft_dir(tmp_path)
    drafts.mkdir(parents=True)
    original = drafts / "original.md"
    original.write_text("draft body\n", encoding="utf-8")
    draft_path = source_probe._draft_path(drafts, SOURCE_URL)
    draft_path.hardlink_to(original)

    with pytest.raises(SourceCandidateLedgerError, match="hard link"):
        run_source_candidate_probe([SOURCE_URL], ledger_path=ledger, feasibility_dir=drafts, opener=NoNetwork())
    assert original.read_text(encoding="utf-8") == "draft body\n"


def test_stale_stage_symlink_is_never_followed(tmp_path: Path) -> None:
    ledger = _ledger_path(tmp_path)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "victim.txt"
    target.write_text("victim\n", encoding="utf-8")
    (ledger.parent / f".{ledger.name}.stage").symlink_to(target)
    (ledger.parent / f".{ledger.name}.backup").symlink_to(target)

    transport = FakeTransport(_accepted_plan())
    report, code = run_source_candidate_probe(
        [SOURCE_URL], ledger_path=ledger, feasibility_dir=_draft_dir(tmp_path), opener=transport
    )

    assert code == 0
    assert target.read_text(encoding="utf-8") == "victim\n"
    data = json.loads(ledger.read_text(encoding="utf-8"))
    assert data["version"] == 3
    assert data["total_entry_count"] == 1


def test_draft_write_failure_leaves_no_partial_draft(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = _ledger_path(tmp_path)
    drafts = _draft_dir(tmp_path)
    real_write_text = Path.write_text

    def failing_write_text(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name.endswith(".tmp"):
            raise OSError("injected draft write failure")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing_write_text)
    with pytest.raises(OSError, match="injected draft write failure"):
        run_source_candidate_probe(
            [SOURCE_URL], ledger_path=ledger, feasibility_dir=drafts, opener=FakeTransport(_accepted_plan())
        )

    assert not list(drafts.glob("*.md"))
    assert not list(drafts.glob("*.tmp"))


def test_probe_with_drafts_does_not_touch_weekly_state(tmp_path: Path) -> None:
    state = tmp_path / "weekly-state"
    state.mkdir()
    state_files = {
        state / "candidate-history.json": b"[]\n",
        state / "candidate-visibility.json": b"{}\n",
        state / "editorial-profile.json": b"[]\n",
    }
    for path, content in state_files.items():
        path.write_bytes(content)
    before = {path: path.read_bytes() for path in state_files}

    transport = FakeTransport(_accepted_plan())
    report, code = run_source_candidate_probe(
        [SOURCE_URL],
        ledger_path=_ledger_path(tmp_path),
        feasibility_dir=_draft_dir(tmp_path),
        opener=transport,
    )
    assert code == 0
    assert report["candidates"][0]["draft_status"] == "created"
    assert {path: path.read_bytes() for path in state_files} == before
    assert not (state / "source-candidate-ledger.json").exists()


# ---------------------------------------------------------------------------
# Public CLI
# ---------------------------------------------------------------------------


def test_cli_accepts_proposing_context_and_feasibility_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    ledger = _ledger_path(tmp_path)
    drafts = _draft_dir(tmp_path)
    monkeypatch.setattr("autoscout.source_probe.urlopen", FakeTransport(_accepted_plan()))
    code = main(
        [
            "probe-source-candidate",
            "--url",
            SOURCE_URL,
            "--proposing-context",
            CONTEXT,
            "--feasibility-dir",
            str(drafts),
            "--ledger",
            str(ledger),
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert code == 0
    assert report["candidates"][0]["proposing_context"] == CONTEXT
    assert report["candidates"][0]["draft_status"] == "created"
    assert list(drafts.glob("*.md"))
    data = json.loads(ledger.read_text(encoding="utf-8"))
    assert data["version"] == 3


def test_cli_default_feasibility_dir_is_safe_and_documented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    ledger = _ledger_path(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("autoscout.source_probe.urlopen", FakeTransport(_accepted_plan()))
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

    assert code == 0
    assert report["candidates"][0]["draft_status"] == "created"
    assert report["candidates"][0]["draft_file"].startswith("data/source-feasibility-drafts/")
    default_dir = tmp_path / "data" / "source-feasibility-drafts"
    assert list(default_dir.glob("*.md"))


def test_cli_rejects_blank_proposing_context_with_contract_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    ledger = _ledger_path(tmp_path)
    network_calls: list[Any] = []

    class NoNetwork:
        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            network_calls.append((args, kwargs))
            raise AssertionError("blank proposing context reached the network")

    monkeypatch.setattr("autoscout.source_probe.urlopen", NoNetwork())
    code = main(
        [
            "probe-source-candidate",
            "--url",
            SOURCE_URL,
            "--proposing-context",
            "   ",
            "--ledger",
            str(ledger),
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert code == 2
    assert "error" in report
    assert "proposing context" in report["error"]
    assert network_calls == []
    assert not ledger.exists()
    assert not ledger.with_suffix(".md").exists()


def test_cli_rejects_too_many_contexts_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    ledger = _ledger_path(tmp_path)
    network_calls: list[Any] = []

    class NoNetwork:
        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            network_calls.append((args, kwargs))
            raise AssertionError("too many contexts reached the network")

    monkeypatch.setattr("autoscout.source_probe.urlopen", NoNetwork())
    code = main(
        [
            "probe-source-candidate",
            "--url",
            SOURCE_URL,
            "--proposing-context",
            CONTEXT,
            "--proposing-context",
            "extra context",
            "--ledger",
            str(ledger),
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert code == 2
    assert "error" in report
    assert network_calls == []
    assert not ledger.exists()
