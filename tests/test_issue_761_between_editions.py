"""Issue #761: an annual page between two editions is a named skip, not a failure.

Deutscher Sachbuchpreis reads its current edition from a stable homepage
(#679). After the award the homepage moves on to the next edition: the call
for entries, then the jury, then key dates. The nominee list, and with it the
``Die Nominierten`` heading the source expects, is gone until the next
nominees are published in spring. From 2026-10-05 every weekly run reported
that as ``page shape validation failed``, which says the source is broken
when nothing is.

These tests pin the boundary. The between-editions state is recognised only
from evidence on the page, the current edition always wins, and a page that
shows neither state still fails closed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autoscout.config import resolve_weekly_portfolio
from autoscout.config import validate_config
from autoscout.json_io import read_json
from autoscout.source_collection import SourceCollection
from autoscout.source_collection import prepare_codex_extraction_worklist
from autoscout.source_page_identity import between_editions_reason
from autoscout.source_page_identity import validate_between_editions_declaration
from autoscout.source_page_identity import validate_page_shape

ROOT = Path(__file__).resolve().parents[1]
PILOT_PATH = ROOT / "configs" / "current-weekly-pilot.json"
SOURCE_NAME = "Deutscher Sachbuchpreis"
# The homepage as served on 2026-10-05, two weeks into the 2027 call.
BETWEEN_EDITIONS_CAPTURE = (
    ROOT / "tests" / "fixtures" / "issue-761-retained-capture" / "deutscher-sachbuchpreis-homepage-2026-10-05.json"
)
CURRENT_EDITION_PAGE = ROOT / "data" / "current-weekly-pilot" / "deutscher-sachbuchpreis-page.json"
EXTRACTION_FIXTURE = ROOT / "data" / "current-weekly-pilot" / "deutscher-sachbuchpreis-extraction.json"
SLOGAN = "Lesen. Denken. Reden."


def _source() -> dict[str, Any]:
    merged = resolve_weekly_portfolio(read_json(PILOT_PATH), PILOT_PATH)
    return dict(next(source for source in merged["sources"] if source.get("name") == SOURCE_NAME))


def _page(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _declared(**overrides: Any) -> dict[str, Any]:
    source = {
        "name": "A Prize",
        "url": "https://example.test/",
        "language": "en",
        "expected_page_heading": "The nominees",
        "between_editions_page_heading": "Call for entries",
        "between_editions_reason": "the next edition is in preparation",
    }
    source.update(overrides)
    return {key: value for key, value in source.items() if value is not None}


# ── The declaration ─────────────────────────────────────────────────────────


def test_the_portfolio_source_declares_its_between_editions_state() -> None:
    source = _source()
    assert source["active"] is True
    assert source["expected_page_heading"] == "Die Nominierten"
    assert source["between_editions_page_heading"] == SLOGAN
    assert source["between_editions_reason"].strip()
    assert validate_between_editions_declaration(source) == ""


def test_a_complete_declaration_is_valid() -> None:
    assert validate_config({"run_date": "2026-10-05", "sources": [_declared()]}) == []


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"between_editions_reason": None}, "must both be nonblank"),
        ({"between_editions_page_heading": None}, "must both be nonblank"),
        ({"between_editions_reason": "  "}, "must both be nonblank"),
        ({"expected_page_heading": None}, "requires expected_page_heading"),
        ({"between_editions_page_heading": " the  NOMINEES "}, "must differ from expected_page_heading"),
    ],
)
def test_an_unusable_declaration_is_refused(overrides: dict[str, Any], fragment: str) -> None:
    errors = validate_config({"run_date": "2026-10-05", "sources": [_declared(**overrides)]})
    assert len(errors) == 1
    assert fragment in errors[0]


# ── Recognising the state from the page ─────────────────────────────────────


def test_the_retained_homepage_is_between_editions() -> None:
    source = _source()
    page = _page(BETWEEN_EDITIONS_CAPTURE)
    # Without the declaration this is exactly the failure the run reported.
    assert validate_page_shape(source, page) == (
        "page shape validation failed: expected page heading 'Die Nominierten' not found"
    )
    assert between_editions_reason(source, page) == source["between_editions_reason"]


def test_the_current_edition_page_is_not_between_editions() -> None:
    source = _source()
    page = _page(CURRENT_EDITION_PAGE)
    assert between_editions_reason(source, page) == ""
    assert validate_page_shape(source, page) == ""


@pytest.mark.parametrize(
    "later_phase",
    [
        # Headings the homepage carried later in the 2026 cycle, after the call
        # closed and before the nominees were published.
        "<h2>2026</h2><h2>Rückblick 2025</h2><h2>Sachbuch des Jahres 2026 gesucht</h2>",
        "<h2>2026</h2><h2>Das ist die Jury</h2><h2>Bücher eingereicht</h2>",
    ],
)
def test_every_phase_before_the_nominees_is_between_editions(later_phase: str) -> None:
    page = {"html": f"<h2>{SLOGAN}</h2>{later_phase}"}
    assert between_editions_reason(_source(), page) != ""


def test_the_current_edition_wins_when_both_headings_appear() -> None:
    page = {"html": f"<h2>{SLOGAN}</h2><h2>Die Nominierten</h2>"}
    assert between_editions_reason(_source(), page) == ""


def test_a_page_showing_neither_state_still_fails_closed() -> None:
    source = _source()
    page = {"html": "<h1>Wartungsarbeiten</h1><p>Lesen. Denken. Reden.</p>"}
    assert between_editions_reason(source, page) == ""
    assert "expected page heading" in validate_page_shape(source, page)


def test_a_hidden_between_editions_heading_does_not_count() -> None:
    page = {"html": f"<div hidden><h2>{SLOGAN}</h2></div><h1>Etwas anderes</h1>"}
    assert between_editions_reason(_source(), page) == ""


def test_a_source_without_the_declaration_is_never_between_editions() -> None:
    source = {key: value for key, value in _source().items() if not key.startswith("between_editions_")}
    assert between_editions_reason(source, _page(BETWEEN_EDITIONS_CAPTURE)) == ""


# ── What the run records ────────────────────────────────────────────────────


def _config_with_page(tmp_path: Path, page_path: Path) -> Path:
    source = _source()
    source["source_page_fixture_file"] = str(page_path.resolve())
    source["extraction_response_file"] = str(EXTRACTION_FIXTURE.resolve())
    for field in ("helper_metadata_file", "helper_metadata_sha256"):
        source.pop(field, None)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"run_date": "2026-10-05", "sources": [source]}), encoding="utf-8")
    return config_path


def test_capture_records_a_named_skip_and_requests_no_extraction(tmp_path: Path) -> None:
    config_path = _config_with_page(tmp_path, BETWEEN_EDITIONS_CAPTURE)
    worklist = prepare_codex_extraction_worklist(config_path, tmp_path / "out", "2026-10-05")

    assert worklist.work_items == []
    assert worklist.failed_source_count == 0
    assert worklist.skipped_source_count == 1
    run = worklist.source_runs[0]
    assert run["status"] == "skipped"
    assert run["reason"] == "between editions: " + _source()["between_editions_reason"]
    assert run["debug_metadata"]["safe_failure_status"] == "between_editions"
    assert not list((tmp_path / "out").glob("codex-capture-*.json"))


def test_capture_still_collects_the_current_edition(tmp_path: Path) -> None:
    config_path = _config_with_page(tmp_path, CURRENT_EDITION_PAGE)
    worklist = prepare_codex_extraction_worklist(config_path, tmp_path / "out", "2026-10-05")

    assert [run["status"] for run in worklist.source_runs] == ["codex-captured"]
    assert len(worklist.work_items) == 1


def test_capture_still_fails_a_page_showing_neither_state(tmp_path: Path) -> None:
    page_path = tmp_path / "page.json"
    broken = _page(CURRENT_EDITION_PAGE)
    broken["html"] = "<h1>Wartungsarbeiten</h1>"
    page_path.write_text(json.dumps(broken), encoding="utf-8")
    worklist = prepare_codex_extraction_worklist(_config_with_page(tmp_path, page_path), tmp_path / "out", "2026-10-05")

    run = worklist.source_runs[0]
    assert run["status"] == "failed"
    assert "expected page heading 'Die Nominierten' not found" in run["reason"]


def test_replay_collection_records_the_same_named_skip(tmp_path: Path) -> None:
    source = _source()
    source["source_page_fixture_file"] = str(BETWEEN_EDITIONS_CAPTURE.resolve())
    source["extraction_response_file"] = str(EXTRACTION_FIXTURE.resolve())
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-10-05").collect([source])

    run = result.source_runs[0]
    assert run["status"] == "skipped"
    assert run["reason"].startswith("between editions: ")
    assert result.source_mentions == []


def test_the_live_fetch_reports_the_same_named_skip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The weekly run reads the page live, and that path validates the page
    shape itself. It has to reach the same verdict as the replay paths."""
    from autoscout.source_collectors import CollectorRunError
    from autoscout.source_collectors import fetch_agent_source_page

    body = _page(BETWEEN_EDITIONS_CAPTURE)["html"].encode("utf-8")
    source = _source()

    class Response:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self, limit: int) -> bytes:
            return body[:limit]

        def geturl(self) -> str:
            return str(source["url"])

        def getheader(self, _name: str, default: str = "") -> str:
            return default

    monkeypatch.setattr("autoscout.source_collectors.urlopen", lambda *_args, **_kwargs: Response())
    with pytest.raises(CollectorRunError) as raised:
        fetch_agent_source_page(tmp_path / "config.json", tmp_path, source)

    assert raised.value.status == "skipped"
    assert str(raised.value) == "between editions: " + source["between_editions_reason"]
    assert raised.value.debug_metadata["safe_failure_status"] == "between_editions"
