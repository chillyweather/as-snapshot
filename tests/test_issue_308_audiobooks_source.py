"""Tests for Issue #308: Fix Audiobooks.com Nonfiction Bestsellers source."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autoscout.config import load_validated_config
from autoscout.run_health import run_health
from autoscout.source_collection import SourceCollection
from autoscout.source_collection import prepare_codex_extraction_worklist
from autoscout.source_collectors import FORBIDDEN_COLLECTOR_RESPONSE_FIELDS
from autoscout.source_collectors import CollectorRunError
from autoscout.source_collectors import agent_extraction_prompt
from autoscout.source_collectors import live_agent_extraction_inputs
from autoscout.source_page_identity import parse_heading_texts
from autoscout.source_page_identity import validate_page_shape

ROOT = Path(__file__).resolve().parents[1]

SOURCE_NAME = "Audiobooks.com Nonfiction Audiobook Bestsellers"
SOURCE_URL = "https://www.audiobooks.com/browse/non-fiction"
SOURCE_FAMILY = "audiobook ranking"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _portfolio_source() -> dict[str, Any]:
    config = load_validated_config(ROOT / "configs/current-weekly-pilot.json")
    source = dict(next(item for item in config["sources"] if item["name"] == SOURCE_NAME))
    source.setdefault("language", "en")
    return source


# ── Portfolio identity ─────────────────────────────────────────────────


def test_portfolio_has_audiobooks_as_canonical_audiobook_ranking_source() -> None:
    portfolio_config = load_validated_config(ROOT / "configs/current-weekly-portfolio.json")
    audiobook_sources = [
        s for s in portfolio_config["sources"] if s.get("family", "").strip().lower() == "audiobook ranking"
    ]
    assert len(audiobook_sources) >= 1
    audiobooks = [s for s in audiobook_sources if "audiobooks.com" in s.get("url", "").lower()]
    assert len(audiobooks) == 1
    assert audiobooks[0]["name"] == SOURCE_NAME
    assert audiobooks[0]["url"] == SOURCE_URL
    assert "portfolio_name" not in audiobooks[0]


def test_pilot_live_overlay_no_longer_needs_portfolio_name_alias() -> None:
    pilot_live = json.loads((ROOT / "configs/current-weekly-pilot-live.json").read_text(encoding="utf-8"))
    audiobooks = [
        s for s in pilot_live["sources"] if s.get("name") == SOURCE_NAME or s.get("portfolio_name") == SOURCE_NAME
    ]
    assert len(audiobooks) == 1, "Expected exactly one Audiobooks overlay in pilot-live config"
    assert audiobooks[0].get("name") == SOURCE_NAME
    assert "portfolio_name" not in audiobooks[0]


def test_audiobooks_source_is_active_open_weekly() -> None:
    source = _portfolio_source()
    assert source.get("active", False) is True
    assert source.get("access_status") == "open"
    assert source.get("check_frequency") == "weekly"
    assert source.get("family") == SOURCE_FAMILY


def test_audiobooks_fixture_files_configured_in_pilot() -> None:
    pilot = load_validated_config(ROOT / "configs/current-weekly-pilot.json")
    source = dict(next(item for item in pilot["sources"] if item["name"] == SOURCE_NAME))
    assert source["extraction_method"] == "codex-extraction-replay"
    fixture = source.get("source_page_fixture_file", "")
    extraction = source.get("extraction_response_file", "")
    assert "audiobooks" in fixture.lower()
    assert "audiobooks" in extraction.lower()
    assert "librofm" not in fixture.lower()
    assert "librofm" not in extraction.lower()


# ── Prompt contract ─────────────────────────────────────────────────────


def test_audiobook_ranking_prompt_includes_exclusion_and_work_type_rules() -> None:
    source = {"name": SOURCE_NAME, "family": "audiobook ranking", "url": SOURCE_URL, "language": "en"}
    page_data = {"html": "<h1>Non-Fiction</h1>"}
    prompt = agent_extraction_prompt(source, page_data, 10_000)
    assert "underlying_work_type" in prompt
    assert "candidate_category" in prompt
    assert "publication_status" in prompt
    assert "forthcoming" in prompt
    assert "Coming Soon" in prompt
    assert "numeric rank" in prompt.lower()
    assert "minor mention" in prompt
    assert "category URL" in prompt
    assert "book-level" in prompt


def test_non_audiobook_prompt_omits_ranking_specific_rules() -> None:
    source = {"name": "Some Award", "family": "award", "url": "https://example.test/award", "language": "en"}
    page_data = {"html": "<h1>Award</h1>"}
    prompt = agent_extraction_prompt(source, page_data, 10_000)
    assert "underlying_work_type" not in prompt
    assert "candidate_category" not in prompt
    assert "publication_status" not in prompt
    assert "numeric rank" not in prompt.lower()


# ── Page-shape validation ───────────────────────────────────────────────


def test_audiobooks_source_has_expected_page_markers() -> None:
    source = _portfolio_source()
    assert source.get("expected_page_heading"), "expected_page_heading must be configured"
    assert source.get("expected_sort_label"), "expected_sort_label must be configured"
    assert "non-fiction" in source["expected_page_heading"].lower()


def test_config_validation_accepts_page_marker_fields() -> None:
    raw = {
        "run_date": "2026-07-17",
        "sources": [
            {
                "name": "test",
                "language": "en",
                "expected_page_heading": "Heading",
                "expected_sort_label": "Sort by: Bestselling",
            }
        ],
    }
    from autoscout.config import validate_config

    errors = validate_config(raw)
    assert not errors, f"Unexpected errors: {errors}"


def test_prepare_fails_closed_on_page_shape_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = {**_portfolio_source(), "url": SOURCE_URL}
    config = tmp_path / "config.json"
    _write_json(config, {"run_date": "2026-07-17", "sources": [source]})

    import autoscout.source_collection as collection_module

    def _missing_markers_page(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return (
            {"source_page": SOURCE_URL, "resolved_url": SOURCE_URL, "html": "<h2>Something Else</h2>"},
            {"resolved_url": SOURCE_URL},
        )

    monkeypatch.setattr(collection_module, "codex_source_page", _missing_markers_page)
    result = prepare_codex_extraction_worklist(config, tmp_path, "2026-07-17")
    assert len(result.work_items) == 0
    failed_run = next(r for r in result.source_runs if r["source_name"] == SOURCE_NAME)
    assert failed_run["status"] == "failed"
    assert failed_run["debug_metadata"]["safe_failure_status"] == "page_shape"


def test_prepare_succeeds_with_correct_page_markers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = {**_portfolio_source(), "url": SOURCE_URL}
    config = tmp_path / "config.json"
    _write_json(config, {"run_date": "2026-07-17", "sources": [source]})

    import autoscout.source_collection as collection_module

    def _valid_markers_page(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return (
            {
                "source_page": SOURCE_URL,
                "resolved_url": SOURCE_URL,
                "html": "<h1>Non-Fiction</h1><span>Sort by: Bestselling</span>",
            },
            {"resolved_url": SOURCE_URL},
        )

    monkeypatch.setattr(collection_module, "codex_source_page", _valid_markers_page)
    result = prepare_codex_extraction_worklist(config, tmp_path, "2026-07-17")
    assert len(result.work_items) == 1
    assert result.work_items[0]["source_name"] == SOURCE_NAME


def test_page_shape_handles_nested_and_void_heading_markup() -> None:
    markup = "<h1>Non-<span>Fic<strong>tion</strong></span><br></h1>"
    assert parse_heading_texts(markup) == ["Non-Fiction"]


def test_page_shape_rejects_hidden_sort_marker() -> None:
    source = {"expected_page_heading": "Non-Fiction", "expected_sort_label": "Sort by: Bestselling"}
    for hidden in ("script", "style", "template", "noscript"):
        page = {"html": f"<h1>Non-Fiction</h1><{hidden}>Sort by: Bestselling</{hidden}>"}
        assert "expected sort label" in validate_page_shape(source, page)


def test_agent_live_validates_target_before_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = {**_portfolio_source(), "enable_live": True}
    source.pop("source_page_fixture_file", None)
    source.pop("live_response_file", None)
    backend_called = False

    def _wrong_target(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return {"resolved_url": "https://www.audiobooks.com/browse", "html": ""}, {}

    def _backend(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        nonlocal backend_called
        backend_called = True
        return {"mentions": []}, {}

    monkeypatch.setattr("autoscout.source_collectors.fetch_agent_source_page", _wrong_target)
    monkeypatch.setattr("autoscout.source_collectors.live_agent_extraction_response", _backend)
    with pytest.raises(CollectorRunError) as caught:
        live_agent_extraction_inputs(tmp_path / "config.json", tmp_path, source)
    assert caught.value.debug_metadata["safe_failure_status"] == "wrong_target"
    assert backend_called is False


def test_agent_live_validates_shape_before_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = {**_portfolio_source(), "enable_live": True}
    source.pop("source_page_fixture_file", None)
    source.pop("live_response_file", None)
    backend_called = False

    def _missing_shape(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return {"resolved_url": SOURCE_URL, "html": "<h1>Browse</h1>"}, {}

    def _backend(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        nonlocal backend_called
        backend_called = True
        return {"mentions": []}, {}

    monkeypatch.setattr("autoscout.source_collectors.fetch_agent_source_page", _missing_shape)
    monkeypatch.setattr("autoscout.source_collectors.live_agent_extraction_response", _backend)
    with pytest.raises(CollectorRunError) as caught:
        live_agent_extraction_inputs(tmp_path / "config.json", tmp_path, source)
    assert caught.value.debug_metadata["safe_failure_status"] == "page_shape"
    assert backend_called is False


def test_agent_live_calls_backend_for_valid_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = {**_portfolio_source(), "enable_live": True}
    source.pop("source_page_fixture_file", None)
    source.pop("live_response_file", None)

    def _valid_page(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        html = "<h1>Non-<span>Fiction</span><br></h1><div>Sort by: Bestselling</div>"
        return {"resolved_url": SOURCE_URL, "source_page": SOURCE_URL, "html": html}, {}

    monkeypatch.setattr("autoscout.source_collectors.fetch_agent_source_page", _valid_page)
    monkeypatch.setattr(
        "autoscout.source_collectors.live_agent_extraction_response",
        lambda *_args, **_kwargs: ({"mentions": []}, {"backend_mode": "test"}),
    )
    _, extraction, _ = live_agent_extraction_inputs(tmp_path / "config.json", tmp_path, source)
    assert extraction == {"mentions": []}


def test_sources_without_page_markers_are_unaffected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = {
        "name": "No Markers",
        "family": "award",
        "url": "https://example.test/nomarkers",
        "active": True,
        "access_status": "open",
        "check_frequency": "weekly",
        "language": "en",
        "extraction_method": "codex-extraction-replay",
    }
    config = tmp_path / "config.json"
    _write_json(config, {"run_date": "2026-07-17", "sources": [source]})

    import autoscout.source_collection as collection_module

    def _any_page(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return (
            {
                "source_page": "https://example.test/nomarkers",
                "resolved_url": "https://example.test/nomarkers",
                "html": "<h1>Stuff</h1>",
            },
            {"resolved_url": "https://example.test/nomarkers"},
        )

    monkeypatch.setattr(collection_module, "codex_source_page", _any_page)
    result = prepare_codex_extraction_worklist(config, tmp_path, "2026-07-17")
    assert len(result.work_items) == 1


def test_page_shape_requires_heading_element_not_nav_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = {**_portfolio_source(), "url": SOURCE_URL}
    config = tmp_path / "config.json"
    _write_json(config, {"run_date": "2026-07-17", "sources": [source]})

    import autoscout.source_collection as collection_module

    def _generic_with_nav(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return (
            {
                "source_page": SOURCE_URL,
                "resolved_url": SOURCE_URL,
                "html": "<h1>Browse Audiobooks</h1><nav>"
                "<a href='/browse/non-fiction'>Non-Fiction</a></nav>"
                "<span>Sort by: Bestselling</span>",
            },
            {"resolved_url": SOURCE_URL},
        )

    monkeypatch.setattr(collection_module, "codex_source_page", _generic_with_nav)
    result = prepare_codex_extraction_worklist(config, tmp_path, "2026-07-17")
    assert len(result.work_items) == 0
    failed_run = next(r for r in result.source_runs if r["source_name"] == SOURCE_NAME)
    assert failed_run["debug_metadata"]["safe_failure_status"] == "page_shape"


def test_page_shape_fails_on_missing_sort_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = {**_portfolio_source(), "url": SOURCE_URL}
    config = tmp_path / "config.json"
    _write_json(config, {"run_date": "2026-07-17", "sources": [source]})

    import autoscout.source_collection as collection_module

    def _heading_but_no_sort(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return (
            {
                "source_page": SOURCE_URL,
                "resolved_url": SOURCE_URL,
                "html": "<h1>Non-Fiction</h1><span>Sort by: New Arrivals</span>",
            },
            {"resolved_url": SOURCE_URL},
        )

    monkeypatch.setattr(collection_module, "codex_source_page", _heading_but_no_sort)
    result = prepare_codex_extraction_worklist(config, tmp_path, "2026-07-17")
    assert len(result.work_items) == 0
    failed_run = next(r for r in result.source_runs if r["source_name"] == SOURCE_NAME)
    assert failed_run["debug_metadata"]["safe_failure_status"] == "page_shape"


def test_page_shape_fails_at_replay_on_generic_heading(tmp_path: Path) -> None:
    capture = {
        "source_name": SOURCE_NAME,
        "source_url": SOURCE_URL,
        "resolved_url": SOURCE_URL,
        "html": "<h1>Browse Audiobooks</h1><nav><a href='/browse/non-fiction'>Non-Fiction</a>"
        "</nav><span>Sort by: Bestselling</span>",
    }
    extraction = {
        "mentions": [
            {
                "title": "Test",
                "primary_authors": ["A"],
                "source_url": SOURCE_URL,
                "evidence_url": SOURCE_URL,
                "signal_type": "audiobook ranking",
                "signal_strength": "minor mention",
                "evidence_note": "Test.",
            }
        ]
    }
    capture_path = tmp_path / "capture.json"
    extraction_path = tmp_path / "extraction.json"
    _write_json(capture_path, capture)
    _write_json(extraction_path, extraction)
    replay_source = {
        "name": SOURCE_NAME,
        "family": SOURCE_FAMILY,
        "url": SOURCE_URL,
        "active": True,
        "access_status": "open",
        "check_frequency": "weekly",
        "language": "en",
        "expected_page_heading": "Non-Fiction",
        "expected_sort_label": "Sort by: Bestselling",
        "extraction_method": "codex-extraction-replay",
        "source_page_fixture_file": str(capture_path),
        "extraction_response_file": str(extraction_path),
    }
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-07-17").collect([replay_source])
    assert result.source_runs[0]["debug_metadata"]["safe_failure_status"] == "page_shape"


# ── Wrong-target regression ─────────────────────────────────────────────


def test_wrong_target_regression_generic_browse_redirect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = {**_portfolio_source(), "url": SOURCE_URL}
    config = tmp_path / "config.json"
    _write_json(config, {"run_date": "2026-07-17", "sources": [source]})

    import autoscout.source_collection as collection_module

    def _redirect_to_browse(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return (
            {
                "source_page": SOURCE_URL,
                "resolved_url": "https://www.audiobooks.com/browse",
                "html": "<h1>Browse Audiobooks</h1>",
            },
            {"resolved_url": "https://www.audiobooks.com/browse"},
        )

    monkeypatch.setattr(collection_module, "codex_source_page", _redirect_to_browse)
    result = prepare_codex_extraction_worklist(config, tmp_path, "2026-07-17")
    assert len(result.work_items) == 0
    failed_run = next(r for r in result.source_runs if r["source_name"] == SOURCE_NAME)
    assert failed_run["debug_metadata"]["safe_failure_status"] == "wrong_target"


def test_wrong_target_regression_at_replay_seam(tmp_path: Path) -> None:
    capture = {
        "source_name": SOURCE_NAME,
        "source_url": SOURCE_URL,
        "resolved_url": "https://www.audiobooks.com/browse",
        "html": "<h1>Browse</h1>",
    }
    extraction = {
        "mentions": [
            {
                "title": "Test Book",
                "primary_authors": ["Test Author"],
                "evidence_url": "https://www.audiobooks.com/audiobook/test",
                "signal_type": "audiobook ranking",
                "signal_strength": "minor mention",
                "evidence_note": "Test note.",
            }
        ]
    }
    capture_path = tmp_path / "capture.json"
    extraction_path = tmp_path / "extraction.json"
    _write_json(capture_path, capture)
    _write_json(extraction_path, extraction)
    replay_source = {
        "name": SOURCE_NAME,
        "family": SOURCE_FAMILY,
        "url": SOURCE_URL,
        "active": True,
        "access_status": "open",
        "check_frequency": "weekly",
        "language": "en",
        "extraction_method": "codex-extraction-replay",
        "source_page_fixture_file": str(capture_path),
        "extraction_response_file": str(extraction_path),
    }
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-07-17").collect([replay_source])
    assert result.source_runs[0]["debug_metadata"]["safe_failure_status"] == "wrong_target"


# ── Fixture audit ────────────────────────────────────────────────────────


def _load_pilot_audiobooks_fixtures() -> tuple[dict[str, Any], Any]:
    pilot_config = load_validated_config(ROOT / "configs/current-weekly-pilot.json")
    source = dict(next(item for item in pilot_config["sources"] if item["name"] == SOURCE_NAME))
    page_file = Path(ROOT / "configs" / source["source_page_fixture_file"])
    extraction_file = Path(ROOT / "configs" / source["extraction_response_file"])
    return (
        json.loads(page_file.read_text(encoding="utf-8")),
        json.loads(extraction_file.read_text(encoding="utf-8")),
    )


def test_capture_fixture_contains_expected_markers() -> None:
    page_data, _ = _load_pilot_audiobooks_fixtures()
    html = str(page_data.get("html", "")).lower()
    assert "non-fiction" in html
    assert "sort by: bestselling" in html


def test_capture_fixture_has_valid_resolved_url() -> None:
    page_data, _ = _load_pilot_audiobooks_fixtures()
    from autoscout.source_proposals import normalize_source_identity

    assert normalize_source_identity(str(page_data.get("resolved_url", ""))) == normalize_source_identity(SOURCE_URL)


def test_capture_fixture_contains_representative_book_cards() -> None:
    page_data, _ = _load_pilot_audiobooks_fixtures()
    html = str(page_data.get("html", ""))
    assert "Atomic Habits" in html
    assert "Everything Is Tuberculosis" in html


def test_capture_fixture_has_real_fiction_leakage_examples() -> None:
    page_data, _ = _load_pilot_audiobooks_fixtures()
    html = str(page_data.get("html", ""))
    assert "All Her Little Lies" in html
    assert "Kindred" in html
    assert 'data-bookid="632353"' in html


def test_extraction_excludes_capture_noise_titles() -> None:
    """Extraction must NOT include any of the explicit exclusion-card titles from capture."""
    page_data, extraction_data = _load_pilot_audiobooks_fixtures()
    capture_html = str(page_data.get("html", ""))
    mentions = extraction_data.get("mentions", [])
    assert len(mentions) == 2, "Expected exactly 2 valid mentions"

    excluded_titles = ["All Her Little Lies", "Kindred"]
    # Each excluded title must be present in capture (prove the noise exists).
    for excluded in excluded_titles:
        assert excluded in capture_html, f"Excluded title '{excluded}' must appear in capture HTML"
    extraction_titles = {str(m.get("title", "")) for m in mentions}
    for excluded in excluded_titles:
        assert excluded not in extraction_titles

    for mention in mentions:
        title = str(mention.get("title", "")).strip()
        authors = mention.get("primary_authors") or mention.get("authors") or []
        assert title
        assert isinstance(authors, list) and len(authors) >= 1
        assert all(isinstance(a, str) and a.strip() for a in authors)

        assert mention.get("underlying_work_type", "").lower() in {"book", "work"}
        assert mention.get("signal_strength") == "minor mention"
        assert mention.get("source_url") == SOURCE_URL
        assert mention.get("publication_status") == "published"

        evidence_url = str(mention.get("evidence_url") or mention.get("item_url") or "")
        assert "/audiobook/" in evidence_url
        assert evidence_url in capture_html

        evidence = str(mention.get("evidence_note", "")).lower()
        for forbidden in (
            "rank #",
            "ranked #",
            "sold",
            "sales",
            "units",
            "copies",
            "week of",
            "first place",
            "second place",
            "third place",
            "position #",
            "debut at",
        ):
            assert forbidden not in evidence


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"underlying_work_type": "podcast"}, "underlying work"),
        ({"candidate_category": "Teen Fiction"}, "excluded"),
        ({"publication_status": "coming soon"}, "forthcoming"),
        ({"signal_strength": "top ranking"}, "strength"),
        ({"evidence_note": "Ranked #1 with 2,000 units sold."}, "rank"),
        ({"evidence_note": "No. 1 for the week ending July 17."}, "rank"),
        ({"evidence_note": "The number one audiobook."}, "rank"),
        ({"title": "All Her Little Lies: A psychological thriller"}, "excluded"),
        ({"source_url": "https://www.audiobooks.com/browse"}, "category URL"),
        ({"evidence_url": SOURCE_URL, "item_url": SOURCE_URL}, "book-level"),
    ],
)
def test_malformed_audiobook_entries_are_dropped(tmp_path: Path, override: dict[str, str], reason: str) -> None:
    page_data, extraction_data = _load_pilot_audiobooks_fixtures()
    valid = dict(extraction_data["mentions"][0])
    malicious = {**valid, "title": "Malicious entry", **override}
    capture_path = tmp_path / "capture.json"
    extraction_path = tmp_path / "extraction.json"
    _write_json(capture_path, page_data)
    _write_json(extraction_path, {"mentions": [valid, malicious]})
    source = {
        **_portfolio_source(),
        "source_page_fixture_file": str(capture_path),
        "extraction_response_file": str(extraction_path),
    }
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-07-17").collect([source])
    run = result.source_runs[0]
    assert run["raw_mention_count"] == 2
    assert run["valid_mention_count"] == 1
    assert run["skipped_item_count"] == 1
    assert reason.casefold() in run["drop_reasons"][0]["reason"].casefold()


def test_extraction_fixture_has_no_copied_text_fields() -> None:
    _, extraction_data = _load_pilot_audiobooks_fixtures()
    for mention in extraction_data.get("mentions", []):
        assert not (set(mention) & FORBIDDEN_COLLECTOR_RESPONSE_FIELDS)


def test_extraction_fixture_has_no_duplicate_mentions() -> None:
    _, extraction_data = _load_pilot_audiobooks_fixtures()
    mentions = extraction_data.get("mentions", [])
    seen: set[tuple[str, str]] = set()
    for mention in mentions:
        title = str(mention.get("title", "")).strip().lower()
        raw_authors = mention.get("primary_authors") or mention.get("authors") or []
        authors_str = (
            ",".join(sorted(str(a).strip().lower() for a in raw_authors))
            if isinstance(raw_authors, list)
            else str(raw_authors).strip().lower()
        )
        key = (title, authors_str)
        assert key not in seen, f"Duplicate: {mention.get('title')}"
        seen.add(key)


# ── Source metrics + run health ──────────────────────────────────────────


def test_audiobooks_source_produces_audiobook_ranking_metrics(tmp_path: Path) -> None:
    page_data, extraction_data = _load_pilot_audiobooks_fixtures()
    capture_path = tmp_path / "capture.json"
    extraction_path = tmp_path / "extraction.json"
    _write_json(capture_path, page_data)
    _write_json(extraction_path, extraction_data)
    replay_source = {
        "name": SOURCE_NAME,
        "family": SOURCE_FAMILY,
        "url": SOURCE_URL,
        "active": True,
        "access_status": "open",
        "check_frequency": "weekly",
        "language": "en",
        "extraction_method": "codex-extraction-replay",
        "source_page_fixture_file": str(capture_path),
        "extraction_response_file": str(extraction_path),
    }
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-07-17").collect([replay_source])
    run = result.source_runs[0]
    assert run["status"] == "collected"
    assert run["family"] == "audiobook ranking"
    assert run["debug_metadata"]["safe_failure_status"] == "healthy"
    assert run["valid_mention_count"] == 2


def test_run_health_shows_audiobook_ranking_with_repaired_source(tmp_path: Path) -> None:
    page_data, extraction_data = _load_pilot_audiobooks_fixtures()
    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    capture_path = output_dir / "audiobooks-capture.json"
    extraction_path = output_dir / "audiobooks-nonfiction-bestsellers-extraction.json"
    _write_json(capture_path, page_data)
    _write_json(extraction_path, extraction_data)
    replay_source = {
        "name": SOURCE_NAME,
        "family": SOURCE_FAMILY,
        "url": SOURCE_URL,
        "active": True,
        "access_status": "open",
        "check_frequency": "weekly",
        "language": "en",
        "extraction_method": "codex-extraction-replay",
        "source_page_fixture_file": "audiobooks-capture.json",
        "extraction_response_file": "audiobooks-nonfiction-bestsellers-extraction.json",
        "codex_artifact_dir": ".",
    }
    tmp_config = tmp_path / "config.json"
    _write_json(
        tmp_config,
        {"run_date": "2026-07-17", "sources": [replay_source], "pilot_mode": True, "russian_edition_checks": []},
    )
    from autoscout.cli.weekly import run_mvp

    run_mvp(tmp_config, output_dir)
    metrics_data = json.loads((output_dir / "source-metrics.json").read_text(encoding="utf-8"))
    audiobook_metrics = [
        m for m in metrics_data["metrics"] if m.get("family", "").strip().lower() == "audiobook ranking"
    ]
    assert len(audiobook_metrics) >= 1
    assert any(m["source_name"] == SOURCE_NAME for m in audiobook_metrics)
    assert run_health(output_dir) == 0
