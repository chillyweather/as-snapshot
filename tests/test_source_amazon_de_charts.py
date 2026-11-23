"""Amazon.de Charts Meistgelesene Sachbücher onboarding contract (Issue #378).

The German chart mirrors the US Amazon Charts source from Issue #368 with
three documented differences: the marketplace host and language are
www.amazon.de / de, the dated URL path is the explicit English segment
"mostread/nonfiction" while the page heading is German, and the per-entry
primary-author marker is the German "von" instead of "by". Mentions carry
non-authoritative English and Russian helper fields alongside the
authoritative German originals (Issue #366 policy).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from autoscout.config import load_validated_config
from autoscout.pipeline import build_scouting_candidates
from autoscout.source_collection import SourceCollection
from autoscout.source_collectors import CollectorRunError
from autoscout.source_collectors import agent_extraction_prompt
from autoscout.source_collectors import extraction_contract_violation
from autoscout.source_collectors import fetch_agent_source_page
from autoscout.source_feasibility import evaluate_amazon_charts_feasibility
from autoscout.source_page_identity import amazon_charts_entries
from autoscout.source_page_identity import amazon_charts_mention_is_grounded
from autoscout.source_page_identity import amazon_charts_ranks
from autoscout.source_page_identity import amazon_charts_week_url
from autoscout.source_page_identity import page_title_text
from autoscout.source_page_identity import validate_page_shape

ROOT = Path(__file__).resolve().parents[1]
NAME = "Amazon.de Charts Meistgelesene Sachbücher"
LANDING_URL = "https://www.amazon.de/charts"
DATED_URL = "https://www.amazon.de/charts/2026-08-02/mostread/nonfiction"
WEEK_REFERENCE = date(2026, 8, 4)


def source() -> dict[str, Any]:
    config = load_validated_config(ROOT / "configs/current-weekly-pilot.json")
    source = dict(next(s for s in config["sources"] if s["name"] == NAME))
    for field in ("source_page_fixture_file", "extraction_response_file"):
        source[field] = str((ROOT / "configs" / source[field]).resolve())
    return source


def _page_data() -> dict[str, Any]:
    return json.loads(Path(source()["source_page_fixture_file"]).read_text(encoding="utf-8"))


def _landing_data() -> dict[str, Any]:
    return json.loads(
        (ROOT / "data/current-weekly-pilot/amazon-de-charts-meistgelesene-sachbuecher-landing.json").read_text(
            encoding="utf-8"
        )
    )


def _extraction() -> dict[str, Any]:
    return json.loads(Path(source()["extraction_response_file"]).read_text(encoding="utf-8"))


def test_resolved_configs_contain_the_established_german_chart() -> None:
    for filename in (
        "current-weekly-portfolio.json",
        "current-weekly-pilot.json",
        "current-weekly-pilot-live.json",
        "current-weekly-live.json",
    ):
        config = load_validated_config(ROOT / "configs" / filename)
        selected = [s for s in config["sources"] if s["name"] == NAME]
        assert len(selected) == 1
        assert selected[0]["amazon_charts_weekly"] is True
        assert selected[0]["lifecycle_status"] == "established"
        assert selected[0]["language"] == "de"
        assert selected[0]["country"] == "Germany"
        assert selected[0]["market"] == "Germany"
        assert selected[0]["expected_page_heading"] == "Meistgelesene Sachbücher"
        assert selected[0]["chart_path_segment"] == "mostread/nonfiction"
        assert selected[0]["author_marker"] == "von"
        assert selected[0]["expected_signal_strength"] == "top ranking"
        assert selected[0]["eligible_candidate_category"] == "adult nonfiction"


def test_parser_returns_exactly_twenty_german_entries() -> None:
    page = _page_data()
    html, source_page = page["html"], page["source_page"]
    assert page_title_text(html) == "Meistgelesene Sachbücher | Amazon Charts"
    entries = amazon_charts_entries(html, source_page)
    assert len(entries) == 20
    assert amazon_charts_ranks(html, source_page) == list(range(1, 21))
    titles = [title for _href, title, _context in entries]
    assert titles[0] == "Die LET THEM Theorie"
    assert titles[1] == "Die 1%-Methode – Minimale Veränderung, maximale Wirkung"
    assert "Sapiens: Eine kurze Geschichte der Menschheit" in titles
    assert "Das Café am Rande der Welt" in titles
    assert all(href.startswith(f"{DATED_URL.split('/charts/')[0]}/dp/") for href, _title, _context in entries)
    # Every bounded entry exposes the German "von" author marker with a
    # nonblank author following it.
    assert all("von " in context for _href, _title, context in entries)


def test_german_entries_are_grounded() -> None:
    page = _page_data()
    extraction = _extraction()
    mentions = extraction["mentions"]
    assert len(mentions) == 20
    assert sorted(m["current_rank"] for m in mentions) == list(range(1, 21))
    for mention in mentions:
        assert amazon_charts_mention_is_grounded(
            page["html"],
            page["source_page"],
            rank=mention["current_rank"],
            title=mention["title"],
            authors=mention["primary_authors"],
            evidence_url=mention["evidence_url"],
        )


def test_explicit_english_segment_resolves_with_german_heading() -> None:
    # The German page heading and the English dated path are independent: the
    # segment comes from chart_path_segment, never from the display heading.
    reference = WEEK_REFERENCE
    assert (
        amazon_charts_week_url(
            '<a href="/charts/2026-08-02/mostread/nonfiction?ref=x">Sachbücher</a>',
            LANDING_URL,
            chart_path_segment="mostread/nonfiction",
            expected_page_heading="Meistgelesene Sachbücher",
            reference_date=reference,
        )
        == DATED_URL
    )
    with pytest.raises(ValueError):
        # A landing page carrying only the heading-derived segment must fail
        # closed when the explicit segment is configured.
        amazon_charts_week_url(
            '<a href="/charts/2026-08-02/meistgelesene/sachbücher">falsch</a>',
            LANDING_URL,
            chart_path_segment="mostread/nonfiction",
            expected_page_heading="Meistgelesene Sachbücher",
            reference_date=reference,
        )


def test_week_resolution_picks_the_newest_german_week_link() -> None:
    landing = _landing_data()
    reference = date(2026, 8, 4)
    # The pinned landing fixture carries two week links (2026-07-26 fiction
    # back link and 2026-08-02 nonfiction) plus an absolute Amazon.de link;
    # the resolver must pick the newest in-window nonfiction week.
    assert (
        amazon_charts_week_url(
            landing["html"],
            landing["landing_url"],
            chart_path_segment="mostread/nonfiction",
            expected_page_heading="Meistgelesene Sachbücher",
            reference_date=reference,
        )
        == DATED_URL
    )
    # Relative and absolute Amazon.de links both resolve on the configured host.
    for href in (
        "/charts/2026-08-02/mostread/nonfiction?ref=chrt_bk_dx_intra_nf_rd",
        "https://www.amazon.de/charts/2026-08-02/mostread/nonfiction?ref=chrt_bk_dx_intra_nf_rd_abs",
    ):
        assert (
            amazon_charts_week_url(
                f'<a href="{href}">Sachbücher</a>',
                LANDING_URL,
                chart_path_segment="mostread/nonfiction",
                expected_page_heading="Meistgelesene Sachbücher",
                reference_date=reference,
            )
            == DATED_URL
        )
    # A stale week outside the recency window fails closed.
    with pytest.raises(ValueError):
        amazon_charts_week_url(
            '<a href="/charts/2026-07-12/mostread/nonfiction">zu alt</a>',
            LANDING_URL,
            chart_path_segment="mostread/nonfiction",
            expected_page_heading="Meistgelesene Sachbücher",
            reference_date=reference,
        )


def test_page_shape_requires_german_heading_and_all_twenty_ranks() -> None:
    s = source()
    page = _page_data()
    assert validate_page_shape(s, page) == ""
    fiction = dict(page)
    fiction["html"] = page["html"].replace("Meistgelesene Sachbücher", "Meistgelesene Belletristik")
    error = validate_page_shape(s, fiction)
    assert error and "title" in error
    # A chart with the exact heading but ranks 1..19 only must fail closed.
    incomplete = dict(page)
    incomplete["html"] = page["html"][: page["html"].rfind('<div id="rank20"')]
    assert validate_page_shape(s, incomplete)


def test_feasibility_accepts_recorded_german_evidence() -> None:
    page = _page_data()
    landing = _landing_data()
    landing_html = landing["html"]
    accepted = evaluate_amazon_charts_feasibility(
        source(),
        head_status=405,
        get_status=200,
        resolved_url=page["source_page"],
        html=page["html"],
        landing_html=landing_html,
        author_marker="von",
        reference_date=WEEK_REFERENCE,
    )
    assert accepted.accepted and accepted.use_get_fallback
    # A mixed-case marker is casefolded before matching, so the explicit
    # "Von" override must satisfy the German chart exactly like "von".
    mixed_case = evaluate_amazon_charts_feasibility(
        source(),
        head_status=405,
        get_status=200,
        resolved_url=page["source_page"],
        html=page["html"],
        landing_html=landing_html,
        author_marker="Von",
        reference_date=WEEK_REFERENCE,
    )
    assert mixed_case.accepted
    # Landing evidence is mandatory: an empty capture rejects even when the
    # dated page evidence is valid.
    empty_landing = evaluate_amazon_charts_feasibility(
        source(),
        head_status=405,
        get_status=200,
        resolved_url=page["source_page"],
        html=page["html"],
        landing_html="",
        author_marker="von",
        reference_date=WEEK_REFERENCE,
    )
    assert not empty_landing.accepted and "landing page evidence is required" in empty_landing.reason
    # The English marker must not satisfy the German chart.
    english_marker = evaluate_amazon_charts_feasibility(
        source(),
        head_status=405,
        get_status=200,
        resolved_url=page["source_page"],
        html=page["html"],
        landing_html=landing_html,
        author_marker="by",
        reference_date=WEEK_REFERENCE,
    )
    assert not english_marker.accepted and "nonblank author" in english_marker.reason
    # A missing landing link fails closed.
    missing_link = evaluate_amazon_charts_feasibility(
        source(),
        head_status=405,
        get_status=200,
        resolved_url=page["source_page"],
        html=page["html"],
        landing_html="<title>Meistgelesene Belletristik | Amazon Charts</title><a href='/charts/2026-08-02/mostread/fiction'>falsch</a>",
        author_marker="von",
        reference_date=WEEK_REFERENCE,
    )
    assert not missing_link.accepted and "no recent dated link" in missing_link.reason
    # A fiction heading on the dated page fails closed.
    fiction_html = page["html"].replace("Meistgelesene Sachbücher", "Meistgelesene Belletristik")
    fiction = evaluate_amazon_charts_feasibility(
        source(),
        head_status=405,
        get_status=200,
        resolved_url=page["source_page"],
        html=fiction_html,
        landing_html=landing_html,
        author_marker="von",
        reference_date=WEEK_REFERENCE,
    )
    assert not fiction.accepted and "page title" in fiction.reason
    # A bot wall and an incomplete rank set fail closed.
    bot_wall = evaluate_amazon_charts_feasibility(
        source(),
        head_status=405,
        get_status=200,
        resolved_url=page["source_page"],
        html=page["html"] + "robot check",
        landing_html=landing_html,
        author_marker="von",
        reference_date=WEEK_REFERENCE,
    )
    assert not bot_wall.accepted and "bot wall" in bot_wall.reason
    truncated = page["html"][: page["html"].rfind('<div id="rank20"')]
    truncated_ranks = evaluate_amazon_charts_feasibility(
        source(),
        head_status=405,
        get_status=200,
        resolved_url=page["source_page"],
        html=truncated,
        landing_html=landing_html,
        author_marker="von",
        reference_date=WEEK_REFERENCE,
    )
    assert not truncated_ranks.accepted and "ranks 1 through 20" in truncated_ranks.reason


def test_replay_collects_all_twenty_mentions_and_preserves_helpers() -> None:
    result = SourceCollection(ROOT / "configs/current-weekly-pilot.json", ROOT, "2026-08-04").collect([source()])
    run = result.source_runs[0]
    assert run["status"] == "collected"
    assert run["valid_mention_count"] == 20
    assert [int(m["current_rank"]) for m in result.source_mentions] == list(range(1, 21))
    for mention in result.source_mentions:
        assert mention["source_url"] == DATED_URL
        assert mention["evidence_url"].startswith("https://www.amazon.de/dp/")
        assert mention["signal_type"] == "bestseller/ranking"
        assert mention["signal_strength"] == "top ranking"
        assert mention["candidate_category"] == "adult nonfiction"
        # Authoritative German originals and non-authoritative helpers.
        # Proper-noun titles (Woman on Fire) keep the same English form; the
        # search translation must always differ from the German original.
        assert mention["english_title"]
        assert mention["english_author_display"]
        assert mention["english_annotation"]
        assert mention["search_translation"] and mention["search_translation"] != mention["title"]
        assert mention["author_transliteration_note"]
        assert not ({"description", "review", "promotional_prose", "sales_units"} & mention.keys())
    # weeks_on_list is validated on the raw fixture and does not project into
    # the collected mention record (same as the English chart).
    assert all(m["weeks_on_list"] > 0 for m in _extraction()["mentions"])


def test_helpers_flow_to_candidates_as_non_authoritative() -> None:
    collected = (
        SourceCollection(ROOT / "configs/current-weekly-pilot.json", ROOT, "2026-08-04")
        .collect([source()])
        .source_mentions
    )
    first = next(m for m in collected if m["title"] == "Die LET THEM Theorie")
    assert first["primary_authors"] == ["Mel Robbins & Sawyer Robbins"]
    assert first["english_title"] == "The Let Them Theory"
    candidates, _ = build_scouting_candidates(collected, "2026-08-04")
    candidate = next(c.to_dict() for c in candidates if c.to_dict()["title"] == "Die LET THEM Theorie")
    assert candidate["title"] == "Die LET THEM Theorie"
    assert candidate["primary_authors"] == ["Mel Robbins & Sawyer Robbins"]
    assert candidate["english_title"] == "The Let Them Theory"
    assert candidate["russian_search_notes"]["search_translation"] == "Теория Let Them"
    assert candidate["russian_search_notes"]["author_transliteration_note"] == "Мел Роббинс и Сойер Роббинс"


@pytest.mark.parametrize("value", [{"retained": "value"}, ["retained"], True, None, "unknown", "TODO", "N/A"])
def test_german_strict_chart_rejects_invalid_helper_values(value: Any) -> None:
    mention = dict(_extraction()["mentions"][0])
    mention["english_title"] = value
    reason = extraction_contract_violation(mention, mention, source(), _page_data())
    assert "helper metadata" in reason


def test_german_contract_allows_helpers_but_keeps_forbidden_fields_out() -> None:
    s = source()
    page = _page_data()
    mention = dict(_extraction()["mentions"][0])
    assert extraction_contract_violation(mention, mention, s, page) == ""
    for override in (
        {"description": "copied prose"},
        {"current_rank": 21},
        {"source_url": LANDING_URL},
        {"candidate_category": "fiction"},
        {"signal_strength": "winner"},
        {"evidence_note": "Print sales were high."},
        {"title": "Fabricated title absent from page"},
    ):
        invalid = {**mention, **override}
        assert extraction_contract_violation(invalid, invalid, s, page), override
    # Every non-English strict-chart helper field is required and nonblank.
    for field in ("english_title", "search_translation", "author_transliteration_note"):
        blank = {**mention, field: ""}
        assert "helper metadata is missing" in extraction_contract_violation(blank, blank, s, page)
        removed = dict(mention)
        removed.pop(field)
        assert "helper metadata is missing" in extraction_contract_violation(removed, removed, s, page)
    # The English chart stays helper-free: helper fields are prohibited there.
    us = {
        "name": "Amazon Charts Most Read Nonfiction",
        "family": "bestseller/ranking",
        "url": "https://www.amazon.com/charts",
        "language": "en",
        "amazon_charts_weekly": True,
        "metadata_exact_book_list": True,
        "expected_page_heading": "Most Read Nonfiction",
        "chart_path_segment": "mostread/nonfiction",
        "expected_signal_strength": "top ranking",
        "eligible_candidate_category": "adult nonfiction",
    }
    us_page = {
        "source_page": "https://www.amazon.com/charts/2026-07-26/mostread/nonfiction",
        "html": "<title>x</title>",
    }
    helper = {key: value for key, value in mention.items() if key != "english_title"}
    helper["english_title"] = "The Let Them Theory"
    assert "prohibited fields" in extraction_contract_violation(helper, helper, us, us_page)


def test_prompt_declares_helper_fields_for_the_german_chart() -> None:
    s = source()
    page = _page_data()
    prompt = agent_extraction_prompt(s, page, 1_500_000)
    for field in ("english_title", "english_author_display", "english_annotation", "search_translation"):
        assert f'"{field}"' in prompt
    assert "non-authoritative" in prompt
    assert '"publisher"' not in prompt and '"publication_year"' not in prompt


def test_live_fetch_resolves_german_landing_to_dated_chart(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[str] = []
    landing = _landing_data()
    page = _page_data()

    class _FakeResponse:
        def __init__(self, body: str, resolved_url: str) -> None:
            self.body = body.encode()
            self.resolved_url = resolved_url

        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, limit: int) -> bytes:
            return self.body[:limit]

        def geturl(self) -> str:
            return self.resolved_url

        def getheader(self, _name: str, default: str = "") -> str:
            return default

    def fake_urlopen(request: Any, **_kwargs: Any) -> _FakeResponse:
        seen.append(request.full_url)
        if request.full_url == LANDING_URL:
            return _FakeResponse(landing["html"], LANDING_URL)
        assert request.full_url == DATED_URL
        return _FakeResponse(page["html"], DATED_URL)

    monkeypatch.setattr("autoscout.source_collectors.urlopen", fake_urlopen)
    fetched, debug = fetch_agent_source_page(
        tmp_path / "config.json", tmp_path, source(), reference_date=WEEK_REFERENCE
    )
    assert seen == [LANDING_URL, DATED_URL]
    assert fetched == {
        "source_page": DATED_URL,
        "resolved_url": DATED_URL,
        "reference_date": WEEK_REFERENCE.isoformat(),
        "html": page["html"],
    }
    assert debug["source_url"] == DATED_URL
    assert debug["landing_resolved_url"] == LANDING_URL


def test_live_fetch_rejects_wrong_marketplace_redirect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[str] = []
    landing = _landing_data()
    page = _page_data()

    class _FakeResponse:
        def __init__(self, body: str, resolved_url: str) -> None:
            self.body = body.encode()
            self.resolved_url = resolved_url

        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, limit: int) -> bytes:
            return self.body[:limit]

        def geturl(self) -> str:
            return self.resolved_url

        def getheader(self, _name: str, default: str = "") -> str:
            return default

    def fake_urlopen(request: Any, **_kwargs: Any) -> _FakeResponse:
        seen.append(request.full_url)
        if request.full_url == LANDING_URL:
            return _FakeResponse(landing["html"], "https://www.amazon.com/charts")
        return _FakeResponse(page["html"], DATED_URL)

    monkeypatch.setattr("autoscout.source_collectors.urlopen", fake_urlopen)
    with pytest.raises(CollectorRunError) as exc_info:
        fetch_agent_source_page(tmp_path / "config.json", tmp_path, source(), reference_date=WEEK_REFERENCE)
    assert seen == [LANDING_URL]
    assert exc_info.value.debug_metadata["safe_failure_status"] == "wrong_target"
