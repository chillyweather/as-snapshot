"""Issue #927: a precollected source keeps how it was collected in its run row.

Live Nonfiction.fr runs are captured live, then replayed from a recorded
artifact. The replayed row used to overwrite the capture row and report the
source as `fixture-replay` / `recorded`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.request import Request

import pytest

import autoscout.source_collectors as collectors_module
from autoscout.codex_workflow import finalize_codex_run
from autoscout.codex_workflow import prepare_codex_extraction
from autoscout.json_io import read_json
from autoscout.pipeline import run_pipeline
from tests.fixtures import copy_fixture

RUN_DATE = "2026-07-17"
FIXTURES = Path(__file__).parent / "fixtures/nonfiction-fr"
FEED = "https://www.nonfiction.fr/rss-critiques.xml"


class _Response:
    def __init__(self, url: str, body: str) -> None:
        self._url = url
        self._body = body.encode("utf-8")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._url


def _source(tmp_path: Path, method: str) -> dict[str, Any]:
    source: dict[str, Any] = {
        "name": "Nonfiction.fr Critiques",
        "family": "media/curated list",
        "url": FEED,
        "active": True,
        "access_status": "open",
        "check_frequency": "weekly",
        "language": "fr",
        "extraction_method": method,
        "seen_guids_file": str(tmp_path / "seen.json"),
    }
    if method == "nonfiction-fr-rss-live":
        source.update(enable_live=True, timeout_seconds=5)
    else:
        source["rss_fixture_file"] = str(copy_fixture("nonfiction-fr", "rss-mixed.xml", tmp_path / "rss.xml"))
        source["review_pages_fixture_file"] = str(
            copy_fixture("nonfiction-fr", "review-pages-mixed.json", tmp_path / "pages.json")
        )
    return source


def _final_row(tmp_path: Path, method: str) -> dict[str, Any]:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"run_date": RUN_DATE, "sources": [_source(tmp_path, method)]}, ensure_ascii=False),
        encoding="utf-8",
    )
    output = tmp_path / "output"
    assert prepare_codex_extraction(config, output, effective_date=RUN_DATE) == 0
    assert finalize_codex_run(output, config, effective_date=RUN_DATE) == 0
    run_pipeline(output / "codex-replay-config.json", output, state_dir=tmp_path / "state")
    (row,) = read_json(output / "source-runs.json")
    return row


def test_a_live_capture_reports_live_after_the_recorded_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rss = (FIXTURES / "rss-mixed.xml").read_text(encoding="utf-8")
    pages = json.loads((FIXTURES / "review-pages-mixed.json").read_text(encoding="utf-8"))["pages"]

    def fake_urlopen(request: Request, timeout: float) -> _Response:
        url = request.full_url
        return _Response(url, rss if url == FEED else pages[url]["html"])

    monkeypatch.setattr(collectors_module, "urlopen", fake_urlopen)

    row = _final_row(tmp_path, "nonfiction-fr-rss-live")

    assert row["collection_mode"] == "live"
    assert row["extraction_method"] == "nonfiction-fr-rss-live"
    assert row["valid_mention_count"] == 1


def test_a_replay_capture_still_reports_replay(tmp_path: Path) -> None:
    row = _final_row(tmp_path, "nonfiction-fr-rss-replay")

    assert row["collection_mode"] == "replay"
    assert row["extraction_method"] == "nonfiction-fr-rss-replay"
