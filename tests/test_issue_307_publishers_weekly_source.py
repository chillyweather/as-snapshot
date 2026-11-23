from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from autoscout.config import load_validated_config
from autoscout.config import validate_config
from autoscout.pipeline import build_scouting_candidates
from autoscout.run_health import run_health
from autoscout.source_collection import SourceCollection
from autoscout.source_collectors import CollectorRunError
from autoscout.source_collectors import _bounded_response_text
from autoscout.source_collectors import agent_extraction_prompt
from autoscout.source_collectors import fetch_agent_source_page
from autoscout.source_page_identity import ranking_mention_is_grounded
from autoscout.source_page_identity import validate_page_shape

ROOT = Path(__file__).resolve().parents[1]
NAME = "Publishers Weekly Hardcover Frontlist Nonfiction"
URL = "https://www.publishersweekly.com/pw/nielsen/HardcoverNonfiction.html"
CONFIGS = (
    "current-weekly-portfolio.json",
    "current-weekly-pilot.json",
    "current-weekly-pilot-live.json",
    "current-weekly-live.json",
)


def source() -> dict:
    config = load_validated_config(ROOT / "configs/current-weekly-pilot.json")
    item = dict(next(value for value in config["sources"] if value["name"] == NAME))
    for field in ("source_page_fixture_file", "extraction_response_file"):
        item[field] = str((ROOT / "configs" / item[field]).resolve())
    return item


def test_config_parity_and_contract() -> None:
    positions = []
    for filename in CONFIGS:
        data = load_validated_config(ROOT / "configs" / filename)
        names = [item["name"] for item in data["sources"]]
        assert names.count(NAME) == 1
        positions.append(names.index(NAME))
    assert len(set(positions)) == 1
    item = source()
    assert item["active"] is True
    assert item["access_status"] == "open"
    assert item["check_frequency"] == "weekly"
    assert item["family"] == "bestseller/ranking"
    assert item["metadata_ranking_table"] is True


def test_ranking_page_contract_survives_worklist_and_replay(tmp_path: Path) -> None:
    from autoscout.codex_workflow import finalize_codex_run
    from autoscout.source_metadata import portable_source_metadata

    configured = source()
    metadata = portable_source_metadata(configured)
    for field in ("expected_table_headers", "expected_visible_markers"):
        assert metadata[field] == configured[field]
    (tmp_path / "codex-extraction-worklist.json").write_text(
        json.dumps(
            {
                "run_date": "2026-09-07",
                "work_items": [
                    {
                        "source_name": NAME,
                        "source_url": URL,
                        "source_family": "bestseller/ranking",
                        "source_metadata": metadata,
                        "capture_file": "capture.json",
                        "expected_extraction_file": "extraction.json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert finalize_codex_run(tmp_path) == 0
    replay = load_validated_config(tmp_path / "codex-replay-config.json")
    for field in ("expected_table_headers", "expected_visible_markers"):
        assert replay["sources"][0][field] == configured[field]


def test_prompt_has_opt_in_metadata_only_ranking_shape() -> None:
    prompt = agent_extraction_prompt(source(), {"html": "table"}, 1000)
    for value in ("current_rank", "source_week", "ISBN", "current-week units", "YTD units"):
        assert value in prompt
    assert 'content_description"' not in prompt
    assert "Write every content_description as concise original Russian prose" not in prompt


def test_ordinary_source_prompt_still_requests_content_description() -> None:
    prompt = agent_extraction_prompt(
        {"name": "Ordinary Source", "family": "award", "url": "https://example.test/award", "language": "en"},
        {"html": "<h1>Award</h1>"},
        1000,
    )
    assert "Write every content_description as concise original Russian prose" in prompt
    assert 'content_description"' in prompt


def test_replay_retains_ranking_metadata_in_source_evidence(tmp_path: Path) -> None:
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-07-17").collect([source()])
    assert result.source_runs[0]["status"] == "collected"
    assert result.source_runs[0]["valid_mention_count"] == 2
    mention = result.source_mentions[0]
    assert mention["isbn"]
    assert mention["current_rank"] == "1"
    assert mention["source_week"] == "Jul. 11, 2026"
    candidates, _ = build_scouting_candidates(result.source_mentions, "2026-07-17")
    evidence = candidates[0].to_dict()["source_evidence"][0]
    assert evidence["current_rank"] == "1"
    assert evidence["source_week"] == "Jul. 11, 2026"


def test_replay_fails_page_shape_and_drops_malformed_rows(tmp_path: Path) -> None:
    configured = source()
    bad_page = tmp_path / "page.json"
    bad_extract = tmp_path / "extract.json"
    bad_page.write_text(json.dumps({"resolved_url": URL, "html": "<h1>Reviews</h1>"}))
    bad_extract.write_text(json.dumps({"mentions": []}))
    failed = SourceCollection(tmp_path / "config.json", tmp_path, "2026-07-17").collect(
        [{**configured, "source_page_fixture_file": str(bad_page), "extraction_response_file": str(bad_extract)}]
    )
    assert failed.source_runs[0]["debug_metadata"]["safe_failure_status"] == "page_shape"

    fixture = json.loads(
        (
            ROOT / "data/current-weekly-pilot/publishers-weekly-hardcover-frontlist-nonfiction-extraction.json"
        ).read_text()
    )
    fixture["mentions"].append({**fixture["mentions"][0], "title": "Bad", "isbn": ""})
    bad_extract.write_text(json.dumps(fixture))
    good_page = ROOT / "data/current-weekly-pilot/publishers-weekly-hardcover-frontlist-nonfiction-page.json"
    dropped = SourceCollection(tmp_path / "config.json", tmp_path, "2026-07-17").collect(
        [{**configured, "source_page_fixture_file": str(good_page), "extraction_response_file": str(bad_extract)}]
    )
    assert dropped.source_runs[0]["valid_mention_count"] == 2
    assert dropped.source_runs[0]["skipped_item_count"] == 1


def test_fixture_is_bounded_and_omits_sales_values_and_reviews() -> None:
    page = json.loads(
        (ROOT / "data/current-weekly-pilot/publishers-weekly-hardcover-frontlist-nonfiction-page.json").read_text()
    )
    extraction = json.loads(
        (
            ROOT / "data/current-weekly-pilot/publishers-weekly-hardcover-frontlist-nonfiction-extraction.json"
        ).read_text()
    )
    assert len(extraction["mentions"]) == 2
    assert len(page["html"]) < 3000
    for mention in extraction["mentions"]:
        assert not ({"review", "description", "units", "ytd", "sales_history"} & set(mention))


def test_bounded_gzip_response_decoding_and_bomb_rejection() -> None:
    class Response:
        def __init__(self, body: bytes, encoding: str = "") -> None:
            self.body = body
            self.encoding = encoding

        def read(self, limit: int) -> bytes:
            return self.body[:limit]

        def getheader(self, name: str, default: str = "") -> str:
            return self.encoding if name.casefold() == "content-encoding" else default

    text, decoded = _bounded_response_text(Response(gzip.compress(b"<table>Rank</table>"), "gzip"))
    assert text == "<table>Rank</table>"
    assert decoded is True
    with pytest.raises(CollectorRunError, match="decompressed page exceeds"):
        _bounded_response_text(Response(gzip.compress(b"x" * 750_001)))


def test_table_aware_grounding_rejects_fabricated_rank_and_cross_row_metadata() -> None:
    page = json.loads(
        (ROOT / "data/current-weekly-pilot/publishers-weekly-hardcover-frontlist-nonfiction-page.json").read_text()
    )
    html = page["html"]
    assert not ranking_mention_is_grounded(
        html,
        title="Regime Change: Inside the Imperial Presidency of Donald Trump",
        primary_author="Maggie Haberman",
        isbn="978-1-6680-6724-6",
        current_rank="999",
        source_week="Jul. 11, 2026",
    )
    assert not ranking_mention_is_grounded(
        html,
        title="Regime Change: Inside the Imperial Presidency of Donald Trump",
        primary_author="Eric Metaxas",
        isbn="978-1-63144-088-5",
        current_rank="1",
        source_week="Jul. 11, 2026",
    )
    unrelated = html.replace("<main>", "<main><article>Rank 999 Maggie Haberman 978-1-6680-6724-6</article>")
    assert not ranking_mention_is_grounded(
        unrelated,
        title="Regime Change: Inside the Imperial Presidency of Donald Trump",
        primary_author="Maggie Haberman",
        isbn="978-1-6680-6724-6",
        current_rank="999",
        source_week="Jul. 11, 2026",
    )


def test_table_headers_must_be_in_one_actual_table_row() -> None:
    configured = source()
    words_only = " ".join(configured["expected_table_headers"] + configured["expected_visible_markers"])
    assert "expected headers" in validate_page_shape(configured, {"html": f"<article>{words_only}</article>"})


def test_ranking_contract_rejects_prohibited_fields(tmp_path: Path) -> None:
    page = ROOT / "data/current-weekly-pilot/publishers-weekly-hardcover-frontlist-nonfiction-page.json"
    base = json.loads(
        (
            ROOT / "data/current-weekly-pilot/publishers-weekly-hardcover-frontlist-nonfiction-extraction.json"
        ).read_text()
    )["mentions"][0]
    for field in (
        "previous_rank",
        "weeks_on_list",
        "publication_date",
        "units",
        "current_week_units",
        "ytd",
        "ytd_units",
        "sales_history",
        "review",
        "description",
    ):
        extraction = tmp_path / f"{field}.json"
        extraction.write_text(json.dumps({"mentions": [{**base, field: "forbidden"}]}))
        result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-07-17").collect(
            [{**source(), "source_page_fixture_file": str(page), "extraction_response_file": str(extraction)}]
        )
        run = result.source_runs[0]
        assert run["valid_mention_count"] == 0
        reasons = [item["reason"] for item in run["drop_reasons"]]
        assert (
            any(field in reason for reason in reasons)
            or run["debug_metadata"]["safe_failure_status"] == "forbidden_copied_text"
        )


def test_config_rejects_malformed_ranking_markers() -> None:
    raw = {
        "run_date": "2026-07-17",
        "sources": [
            {
                "name": "bad",
                "metadata_ranking_table": True,
                "expected_table_headers": ["Rank", ""],
                "expected_visible_markers": [3],
            }
        ],
    }
    errors = validate_config(raw)
    assert any("expected_table_headers" in error for error in errors)
    assert any("expected_visible_markers" in error for error in errors)


def test_dedup_preserves_pw_and_usa_today_evidence() -> None:
    pw = json.loads(
        (
            ROOT / "data/current-weekly-pilot/publishers-weekly-hardcover-frontlist-nonfiction-extraction.json"
        ).read_text()
    )["mentions"][0]
    common = {**pw, "source_check_date": "2026-07-17"}
    mentions = [
        {**common, "source_name": NAME, "source_family": "bestseller/ranking"},
        {
            **common,
            "source_name": "USA TODAY Bestselling Books Nonfiction",
            "source_family": "bestseller/ranking",
            "source_url": "https://www.usatoday.com/booklist/nonfiction",
            "evidence_url": "https://www.usatoday.com/booklist/nonfiction",
        },
    ]
    candidates, _ = build_scouting_candidates(mentions, "2026-07-17")
    assert len(candidates) == 1
    evidence = candidates[0].to_dict()["source_evidence"]
    assert {item["source_name"] for item in evidence} == {NAME, "USA TODAY Bestselling Books Nonfiction"}


def test_pw_source_metrics_and_run_health(tmp_path: Path) -> None:
    from autoscout.cli.weekly import run_mvp

    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"run_date": "2026-07-17", "pilot_mode": True, "sources": [source()], "russian_edition_checks": []})
    )
    output = tmp_path / "output"
    run_mvp(config, output)
    metrics = json.loads((output / "source-metrics.json").read_text())["metrics"]
    metric = next(item for item in metrics if item["source_name"] == NAME)
    assert metric["family"] == "bestseller/ranking"
    assert run_health(output) == 0


def test_fetch_agent_source_page_decodes_unsolicited_gzip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    markup = b"<table><tr><th>Rank</th></tr></table>"

    class Response:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self, limit: int) -> bytes:
            return gzip.compress(markup)[:limit]

        def geturl(self) -> str:
            return URL

        def getheader(self, _name: str, default: str = "") -> str:
            return "gzip"

    monkeypatch.setattr("autoscout.source_collectors.urlopen", lambda *_args, **_kwargs: Response())
    page, debug = fetch_agent_source_page(tmp_path / "config.json", tmp_path, {"url": URL})
    assert page["html"] == markup.decode()
    assert debug["content_decoded"] is True


@pytest.mark.parametrize(
    ("isbn", "grounded"),
    [
        ("978-1-6680-6724-6", True),
        ("9781668067246", True),
        ("ISBN 978-1-6680-6724-6", True),
        ("9781668067247", False),
        ("978-1-6680-6724-6 978-1-63144-088-5", False),
    ],
)
def test_table_row_isbn_is_matched_as_an_isbn_not_as_copied_text(isbn: str, grounded: bool) -> None:
    """Issue #827: the contract asks for bare ISBN digits, and the page shows
    them hyphenated, so the table row is matched on the ISBN it states."""
    page = json.loads(
        (ROOT / "data/current-weekly-pilot/publishers-weekly-hardcover-frontlist-nonfiction-page.json").read_text()
    )
    assert (
        ranking_mention_is_grounded(
            page["html"],
            title="Regime Change: Inside the Imperial Presidency of Donald Trump",
            primary_author="Maggie Haberman",
            isbn=isbn,
            current_rank="1",
            source_week="Jul. 11, 2026",
        )
        is grounded
    )
