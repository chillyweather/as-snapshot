"""Reusable source-onboarding and exact-book-list contract assertions."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from autoscout.annual_edition import ANNUAL_EDITION_FIELD
from autoscout.annual_edition import is_annual_source
from autoscout.annual_edition import validate_annual_edition_declaration
from autoscout.cli.weekly import run_mvp
from autoscout.codex_workflow import finalize_codex_run
from autoscout.codex_workflow import prepare_codex_extraction
from autoscout.helper_metadata import HELPER_METADATA_FIELDS
from autoscout.helper_metadata import validate_helper_metadata
from autoscout.json_io import read_json
from autoscout.pipeline import build_scouting_candidates
from autoscout.run_health import run_health
from autoscout.source_collection import SourceCollection
from autoscout.source_collection import prepare_codex_extraction_worklist
from autoscout.source_collectors import FORBIDDEN_COLLECTOR_RESPONSE_FIELDS
from autoscout.source_collectors import codex_extraction_collector_result
from autoscout.source_metadata import EXACT_BOOK_LIST_FIELDS
from autoscout.source_metadata import SOURCE_METADATA_FIELDS
from autoscout.source_metadata import SOURCE_PAGE_IDENTITY_FIELDS
from autoscout.source_page_identity import exact_book_list_mention_is_grounded
from autoscout.source_page_identity import link_anchor_pairs
from autoscout.source_page_identity import validate_page_shape

RUN_DATE = "2026-07-18"
REQUIRED_ONBOARDING_FIELDS = (
    "name",
    "family",
    "url",
    "active",
    "access_status",
    "check_frequency",
    "language",
    "country",
    "market",
    "expected_category",
    "signal_family",
    "refresh_cadence",
    "url_stability",
    "extraction_notes",
    "lifecycle_status",
    "lifecycle_note",
    "notes",
)
EXACT_BOOK_LIST_REQUIRED_FIELDS = (
    "expected_page_heading",
    "expected_signal_strength",
    "eligible_candidate_category",
)
ALLOWED_LIFECYCLE_STATUSES = {"probation", "established", "retired"}
REPLAY_INPUT_FIXTURE_FIELDS = (
    "source_page_fixture_file",
    "extraction_response_file",
    "recorded_mentions_file",
    "rss_fixture_file",
    "review_pages_fixture_file",
    "seen_guids_fixture_file",
    # Issue #945: the recorded result of the PM deals pass.
    "pm_collection_file",
    # Issue #955: the recorded result of the PW issue pass.
    "pw_collection_file",
)


def assert_source_onboarding_metadata(source: dict[str, Any]) -> None:
    """Assert the runbook metadata contract for one configured source."""
    source_name = source.get("name")
    missing = [field for field in REQUIRED_ONBOARDING_FIELDS if not str(source.get(field, "")).strip()]
    assert not missing, f"source {source_name!r} is missing onboarding metadata: {missing}"
    assert source["lifecycle_status"] in ALLOWED_LIFECYCLE_STATUSES
    if source.get("metadata_exact_book_list"):
        for field in EXACT_BOOK_LIST_REQUIRED_FIELDS:
            assert str(source.get(field, "")).strip(), f"exact source is missing {field}"
    if is_annual_source(source):
        assert_annual_source_metadata(source)


def assert_annual_source_metadata(source: dict[str, Any]) -> None:
    """Assert the #680 annual metadata contract for one annual source."""
    source_name = source.get("name")
    for field in ("url_stability", "refresh_cadence"):
        assert str(source.get(field, "")).strip(), f"annual source {source_name!r} is missing {field}"
    assert source["refresh_cadence"] == "annual", f"annual source {source_name!r} must refresh annually"
    assert ANNUAL_EDITION_FIELD in source, f"annual source {source_name!r} declares no {ANNUAL_EDITION_FIELD}"
    error = validate_annual_edition_declaration(source)
    assert not error, f"annual source {source_name!r}: {error}"


def replay_input_fixture_paths(overlay: dict[str, Any], base_dir: Path) -> list[tuple[str, Path]]:
    """Return committed input fixture paths declared by one replay overlay."""
    paths: list[tuple[str, Path]] = []
    for field in REPLAY_INPUT_FIXTURE_FIELDS:
        raw_values = overlay.get(field + "s", []) if field == "recorded_mentions_file" else []
        if overlay.get(field):
            raw_values = [overlay[field], *raw_values]
        for raw_value in raw_values:
            raw_path = Path(str(raw_value))
            resolved = raw_path if raw_path.is_absolute() else base_dir / raw_path
            paths.append((field, resolved))
    return paths


def assert_replay_overlay_contract(
    source: dict[str, Any],
    overlay: dict[str, Any],
    *,
    base_dir: Path,
    extraction_method: str | None = None,
) -> None:
    """Assert the replay overlay identity and committed fixture contract."""
    target = str(overlay.get("portfolio_name") or overlay.get("name", "")).strip()
    assert target == source["name"]
    if extraction_method is not None:
        assert overlay.get("extraction_method") == extraction_method
    fixture_paths = replay_input_fixture_paths(overlay, base_dir)
    assert fixture_paths, f"replay overlay for {source['name']!r} declares no input fixture files"
    for field, path in fixture_paths:
        assert path.is_file(), f"replay source {source['name']!r}: {field} points at missing file {path}"


@dataclass(frozen=True)
class ExactBookListCase:
    """One source and its local page and extraction fixtures."""

    name: str
    source: dict[str, Any]
    page: dict[str, Any]
    extraction: dict[str, Any]

    def replay_source(self, page_path: Path | None = None, extraction_path: Path | None = None) -> dict[str, Any]:
        source = dict(self.source)
        if page_path is not None:
            source["source_page_fixture_file"] = str(page_path)
        if extraction_path is not None:
            source["extraction_response_file"] = str(extraction_path)
        return source


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def assert_source_onboarding_contract(
    source: dict[str, Any],
    overlay: dict[str, Any],
    *,
    page_fixture: Path,
    extraction_fixture: Path,
) -> None:
    """Assert the runbook contract for one portfolio source and replay overlay."""
    assert_source_onboarding_metadata(source)
    assert_replay_overlay_contract(
        source,
        overlay,
        base_dir=page_fixture.parent,
        extraction_method="codex-extraction-replay",
    )
    assert (page_fixture.parent / overlay["source_page_fixture_file"]).resolve() == page_fixture.resolve()
    assert (extraction_fixture.parent / overlay["extraction_response_file"]).resolve() == extraction_fixture.resolve()
    assert page_fixture.is_file()
    assert extraction_fixture.is_file()


def _assert_helpers(case: ExactBookListCase) -> None:
    """Require the original exact-list helper metadata contract."""
    strict_chart = case.source.get("audible_explicit_chart") is True or case.source.get("amazon_charts_weekly") is True
    is_english = str(case.source.get("language", "")).strip().casefold() in {"en", "english"}
    for mention in case.extraction["mentions"]:
        if strict_chart and is_english:
            assert not set(HELPER_METADATA_FIELDS) & mention.keys()
            continue
        title_relation = None if strict_chart else not is_english
        helper_error = validate_helper_metadata(mention, title_relation=title_relation)
        assert not helper_error, helper_error


def _assert_grounding_and_metadata(case: ExactBookListCase) -> None:
    strict_chart = case.source.get("audible_explicit_chart") is True or case.source.get("amazon_charts_weekly") is True
    for mention in case.extraction["mentions"]:
        if case.source.get("amazon_charts_weekly") is True:
            from autoscout.source_page_identity import amazon_charts_mention_is_grounded

            assert amazon_charts_mention_is_grounded(
                case.page["html"],
                case.page["source_page"],
                rank=mention["current_rank"],
                title=mention["title"],
                authors=mention["primary_authors"],
                evidence_url=mention["evidence_url"],
            )
        else:
            assert exact_book_list_mention_is_grounded(
                case.page["html"],
                case.page["source_page"],
                title=mention["title"],
                authors=mention["primary_authors"],
                evidence_url=mention["evidence_url"],
            )
        assert mention["signal_type"] == case.source.get("signal_family", case.source["family"])
        assert mention["signal_strength"] == case.source["expected_signal_strength"]
        assert mention["candidate_category"] == case.source["eligible_candidate_category"]
        expected_source_url = (
            case.page["source_page"] if case.source.get("amazon_charts_weekly") else case.source["url"]
        )
        assert mention["source_url"] == expected_source_url
        assert not set(mention) & FORBIDDEN_COLLECTOR_RESPONSE_FIELDS
        if strict_chart:
            continue


def _card_rewrap_html(case: ExactBookListCase) -> str:
    """Deterministic card-shaped rewrap of one case's flat fixture entries.

    Every fixture mention becomes one whole book card — a single ``<a>``
    wrapping year, title and author (issue #715), the shape modern listing
    pages serve. The year paragraph carries the mention's
    ``publication_year`` (the run year when the fixture records none), so
    each link text always has the year + title + author shape a live card
    has."""
    cards = []
    for mention in case.extraction["mentions"]:
        year = str(mention.get("publication_year") or "").strip() or RUN_DATE[:4]
        authors = " ".join(str(author).strip() for author in mention.get("primary_authors", []))
        cards.append(
            '<article class="listing"><a class="listing__inner" href="{url}">'
            '<p class="listing__meta">{year}</p>'
            '<h2 class="listing__title">{title}</h2>'
            '<p class="listing__sub">{authors}</p>'
            "</a></article>".format(
                url=escape(str(mention["evidence_url"])),
                year=escape(year),
                title=escape(str(mention["title"])),
                authors=escape(authors),
            )
        )
    return "<!DOCTYPE html><html><body><main>" + "".join(cards) + "</main></body></html>"


def _assert_card_shaped_grounding(case: ExactBookListCase) -> None:
    """Every mention grounds on the card-shaped rewrap exactly as on the flat fixture.

    This is the regression that would have caught issue #715: the same
    entries rewrapped so one link carries year, title and author must ground
    identically to the flat variant. Amazon Charts cases are excluded: their
    grounding rule is rank-based and outside this contract family."""
    if case.source.get("amazon_charts_weekly") is True:
        return
    card_html = _card_rewrap_html(case)
    for mention in case.extraction["mentions"]:
        flat = exact_book_list_mention_is_grounded(
            case.page["html"],
            case.page["source_page"],
            title=mention["title"],
            authors=mention["primary_authors"],
            evidence_url=mention["evidence_url"],
        )
        card = exact_book_list_mention_is_grounded(
            card_html,
            case.page["source_page"],
            title=mention["title"],
            authors=mention["primary_authors"],
            evidence_url=mention["evidence_url"],
        )
        assert card == flat, (
            f"card-shaped rewrap grounds {mention['title']!r} as {card} but the flat fixture grounds it as {flat}"
        )


def _assert_replay_collection(case: ExactBookListCase, tmp_path: Path) -> None:
    result = SourceCollection(tmp_path / "config.json", tmp_path / "replay", RUN_DATE).collect([case.source])
    run = result.source_runs[0]
    assert run["status"] == "collected"
    assert run["debug_metadata"]["safe_failure_status"] == "healthy"
    assert run["valid_mention_count"] == len(case.extraction["mentions"])
    assert len(result.source_mentions) == len(case.extraction["mentions"])
    expected_metadata = {
        "source_name": case.name,
        "source_url": case.page["source_page"] if case.source.get("amazon_charts_weekly") else case.source["url"],
        "language": case.source.get("language"),
        "market": case.source.get("market"),
        "expected_category": case.source.get("expected_category"),
        "signal_type": case.source.get("signal_family", case.source["family"]),
        "signal_strength": case.source["expected_signal_strength"],
        "candidate_category": case.source.get("eligible_candidate_category"),
    }
    for mention in result.source_mentions:
        for field, expected in expected_metadata.items():
            if expected is not None:
                assert mention[field] == expected


def _assert_candidate_preservation(case: ExactBookListCase, tmp_path: Path) -> None:
    if case.source.get("audible_explicit_chart") is True or case.source.get("amazon_charts_weekly") is True:
        return
    result = SourceCollection(tmp_path / "config.json", tmp_path / "candidate-replay", RUN_DATE).collect([case.source])
    candidates, _ = build_scouting_candidates(result.source_mentions, RUN_DATE)
    by_title = {candidate.to_dict()["title"]: candidate.to_dict() for candidate in candidates}
    for mention in case.extraction["mentions"]:
        candidate = by_title[mention["title"]]
        assert candidate["title"] == mention["title"]
        assert candidate["primary_authors"] == mention["primary_authors"]
        assert candidate["english_title"] == mention.get("english_title", "")
        search_notes = candidate["russian_search_notes"]
        assert search_notes.get("search_translation", "") == mention.get("search_translation", "")
        if mention.get("search_translation"):
            assert search_notes["search_translation"] != candidate["title"]
        assert search_notes.get("author_transliteration_note", "") == mention.get("author_transliteration_note", "")
        evidence = candidate["source_evidence"][0]
        assert evidence["expected_category"] == case.source["expected_category"]
        assert evidence["language"] == case.source["language"]
        assert evidence["signal_strength"] == case.source["expected_signal_strength"]


def _assert_prepare_finalize_round_trip(case: ExactBookListCase, tmp_path: Path, monkeypatch: Any) -> None:
    source = {**case.source, "source_page_fixture_file": "", "extraction_response_file": ""}
    work_dir = tmp_path / "prepare-finalize"
    config = work_dir / "config.json"
    write_json(config, {"run_date": RUN_DATE, "sources": [source]})
    import autoscout.source_collection as source_collection

    monkeypatch.setattr(
        source_collection,
        "codex_source_page",
        lambda *_args, **_kwargs: (case.page, {"resolved_url": case.page["resolved_url"]}),
    )
    assert len(prepare_codex_extraction_worklist(config, work_dir, RUN_DATE).work_items) == 1
    assert prepare_codex_extraction(config, work_dir, effective_date=RUN_DATE) == 0
    item = read_json(work_dir / "codex-extraction-worklist.json")["work_items"][0]
    fields = set(EXACT_BOOK_LIST_FIELDS) | set(SOURCE_METADATA_FIELDS) | set(SOURCE_PAGE_IDENTITY_FIELDS)
    for field in fields:
        assert item["source_metadata"].get(field, "") == source.get(field, "")
    write_json(work_dir / item["expected_extraction_file"], {"mentions": []})
    assert finalize_codex_run(work_dir) == 0
    replay = read_json(work_dir / "codex-replay-config.json")["sources"][0]
    for field in fields:
        assert replay.get(field, "") == source.get(field, "")
    collected = SourceCollection(config, work_dir / "round-trip-collection", RUN_DATE).collect([case.source])
    for mention in collected.source_mentions:
        assert not set(SOURCE_PAGE_IDENTITY_FIELDS) & mention.keys()


def _assert_healthy_empty(case: ExactBookListCase, tmp_path: Path) -> None:
    page_path = tmp_path / "empty-page.json"
    write_json(page_path, case.page)
    source_url = case.page["source_page"] if case.source.get("amazon_charts_weekly") else case.source["url"]
    extraction = {
        "source_name": case.name,
        "source_url": source_url,
        "operation_id": "op-contract",
        "capture_sha256": hashlib.sha256(page_path.read_bytes()).hexdigest(),
        "entries": [],
        "mentions": [],
    }
    extraction_path = tmp_path / "empty-extraction.json"
    write_json(extraction_path, extraction)
    source = case.replay_source(page_path, extraction_path)
    result = SourceCollection(tmp_path / "empty-config.json", tmp_path / "empty-collection", RUN_DATE).collect([source])
    strict_chart = case.source.get("audible_explicit_chart") is True or case.source.get("amazon_charts_weekly") is True
    expected_empty_status = "page_shape" if strict_chart else "healthy_empty"
    assert result.source_runs[0]["debug_metadata"]["safe_failure_status"] == expected_empty_status
    extraction.pop("operation_id")
    write_json(extraction_path, extraction)
    result = SourceCollection(tmp_path / "empty-config-2.json", tmp_path / "empty-collection-2", RUN_DATE).collect(
        [source]
    )
    expected_unmarked_status = "page_shape" if strict_chart else "empty_extraction"
    assert result.source_runs[0]["debug_metadata"]["safe_failure_status"] == expected_unmarked_status


def _assert_invalid_mentions(case: ExactBookListCase, tmp_path: Path) -> None:
    if case.source.get("audible_explicit_chart") is True or case.source.get("amazon_charts_weekly") is True:
        return
    overrides = (
        {"title": "Fabricated title absent from page"},
        {"primary_authors": ["Fabricated Author"]},
        {"candidate_category": "fiction"},
        {"candidate_category": "children/YA"},
        {"candidate_category": "narrow manual"},
        {"candidate_category": "topic-only reference"},
        # A strength the source does not declare; a winner-stage source declares "winner" (#996).
        {"signal_strength": "winner" if case.source.get("expected_signal_strength") != "winner" else "longlist"},
        {"signal_type": "bestseller/ranking"},
        {"source_url": "https://foreign.example.test/fake"},
    )
    reasons = (
        "not grounded",
        "not grounded",
        "candidate_category",
        "candidate_category",
        "candidate_category",
        "candidate_category",
        "signal_strength",
        "signal_type",
        "source_url",
    )
    for index, (override, reason) in enumerate(zip(overrides, reasons, strict=True)):
        mention = dict(case.extraction["mentions"][0])
        mention.update(override)
        source = case.replay_source(
            tmp_path / f"invalid-page-{index}.json", tmp_path / f"invalid-extraction-{index}.json"
        )
        write_json(Path(source["source_page_fixture_file"]), case.page)
        write_json(Path(source["extraction_response_file"]), {"mentions": [mention]})
        result = SourceCollection(
            tmp_path / f"invalid-config-{index}.json", tmp_path / f"invalid-output-{index}", RUN_DATE
        ).collect([source])
        run = result.source_runs[0]
        assert run["valid_mention_count"] == 0
        assert reason.casefold() in run["drop_reasons"][0]["reason"].casefold()


def _assert_sibling_isolation(case: ExactBookListCase, tmp_path: Path) -> None:
    bad_page = tmp_path / "bad-page.json"
    bad_extraction = tmp_path / "bad-extraction.json"
    write_json(
        bad_page,
        {
            "source_name": "bad",
            "source_url": "https://bad.test/list",
            "resolved_url": "https://wrong.test/",
            "html": "<h1>Wrong</h1>",
        },
    )
    write_json(bad_extraction, {"mentions": []})
    bad = {
        "name": "Bad sibling",
        "family": "award",
        "url": "https://bad.test/list",
        "active": True,
        "access_status": "open",
        "check_frequency": "annual",
        "extraction_method": "codex-extraction-replay",
        "source_page_fixture_file": str(bad_page),
        "extraction_response_file": str(bad_extraction),
    }
    result = SourceCollection(tmp_path / "siblings-config.json", tmp_path / "siblings-output", RUN_DATE).collect(
        [bad, case.source]
    )
    assert result.source_runs[0]["debug_metadata"]["safe_failure_status"] == "wrong_target"
    assert len(result.source_mentions) == len(case.extraction["mentions"])


def assert_exact_book_list_contract(
    case: ExactBookListCase, tmp_path: Path, monkeypatch: Any, *, run_health_checks: bool = True
) -> None:
    """Run the complete applicable exact-book-list contract battery.

    The battery intentionally uses only temporary files and the same
    production seams as the canonical configured-source contract.
    """
    assert {"source_name", "source_url", "resolved_url", "source_page", "html"} <= case.page.keys()
    assert case.page["source_name"] == case.name
    assert len(case.page["html"]) < 8000
    if case.source.get("amazon_charts_weekly") is True:
        assert case.page["source_url"] == case.page["source_page"]
        assert case.page["resolved_url"] == case.page["source_page"]
        segment = str(case.source.get("chart_path_segment", "")).strip("/")
        host = urlparse(str(case.source["url"])).netloc
        assert segment and host
        assert re.fullmatch(
            rf"https://{re.escape(host)}/charts/\d{{4}}-\d{{2}}-\d{{2}}/{re.escape(segment)}",
            case.page["source_page"],
        )
    else:
        for field in ("source_url", "source_page", "resolved_url"):
            assert case.page[field] == case.source["url"]
    assert validate_page_shape(case.source, case.page) == ""
    count = len(case.extraction["mentions"])
    assert 1 <= count <= len(link_anchor_pairs(case.page["html"], case.page["source_page"]))
    _assert_grounding_and_metadata(case)
    _assert_card_shaped_grounding(case)
    _assert_helpers(case)
    _assert_replay_collection(case, tmp_path)
    _assert_candidate_preservation(case, tmp_path)
    replay_result = codex_extraction_collector_result(
        tmp_path / "collector-config.json", tmp_path / "collector-output", case.source, RUN_DATE
    )
    assert len(replay_result.mentions) == count
    assert replay_result.debug_metadata["safe_failure_status"] == "healthy"
    if run_health_checks:
        output = tmp_path / "health-output"
        source = case.replay_source(output / "page.json", output / "extraction.json")
        write_json(output / "page.json", case.page)
        write_json(output / "extraction.json", case.extraction)
        source["source_page_fixture_file"] = "page.json"
        source["extraction_response_file"] = "extraction.json"
        source["codex_artifact_dir"] = "."
        config = output / "config.json"
        write_json(
            config, {"run_date": RUN_DATE, "sources": [source], "pilot_mode": True, "russian_edition_checks": []}
        )
        run_mvp(config, output)
        metrics = [m for m in read_json(output / "source-metrics.json")["metrics"] if m["source_name"] == case.name]
        assert len(metrics) == 1
        assert metrics[0]["family"] == case.source["family"]
        assert metrics[0]["language"] == case.source["language"]
        assert metrics[0]["lifecycle_status"] == case.source["lifecycle_status"]
        assert run_health(output) == 0
    _assert_prepare_finalize_round_trip(case, tmp_path, monkeypatch)
    _assert_healthy_empty(case, tmp_path)
    _assert_invalid_mentions(case, tmp_path)
    _assert_sibling_isolation(case, tmp_path)
