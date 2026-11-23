"""Audible explicit chart onboarding contract (Issue #314)."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

from autoscout.config import load_validated_config
from autoscout.config import validate_config
from autoscout.pipeline import build_scouting_candidates
from autoscout.source_collection import SourceCollection
from autoscout.source_collectors import CollectorRunError
from autoscout.source_collectors import _bounded_response_text
from autoscout.source_collectors import agent_extraction_prompt
from autoscout.source_collectors import extraction_contract_violation
from autoscout.source_collectors import fetch_agent_source_page
from autoscout.source_collectors import model_bounded_page_data
from autoscout.source_collectors import normalized_agent_mention
from autoscout.source_feasibility import evaluate_audible_chart_feasibility
from autoscout.source_page_identity import audible_chart_mention_is_grounded

ROOT = Path(__file__).resolve().parents[1]
NAMES = (
    "Audible Bestselling Science & Engineering Audiobooks",
    "Audible Bestselling Business & Careers Audiobooks",
    "Audible Bestselling Computers & Technology Audiobooks",
)


def sources() -> list[dict[str, Any]]:
    config = load_validated_config(ROOT / "configs/current-weekly-pilot.json")
    result = []
    for name in NAMES:
        source = dict(next(s for s in config["sources"] if s["name"] == name))
        for field in ("source_page_fixture_file", "extraction_response_file"):
            source[field] = str((ROOT / "configs" / source[field]).resolve())
        result.append(source)
    return result


def test_resolved_configs_contain_three_ordered_established_charts() -> None:
    for filename in (
        "current-weekly-portfolio.json",
        "current-weekly-pilot.json",
        "current-weekly-pilot-live.json",
        "current-weekly-live.json",
    ):
        config = load_validated_config(ROOT / "configs" / filename)
        selected = [s for s in config["sources"] if s["name"] in NAMES]
        assert [s["name"] for s in selected] == list(NAMES)
        assert all(s["audible_explicit_chart"] and s["lifecycle_status"] == "established" for s in selected)


def test_replay_contract_preserves_exact_rank_and_metadata_only() -> None:
    for source in sources():
        result = SourceCollection(ROOT / "configs/current-weekly-pilot.json", ROOT, "2026-07-19").collect([source])
        assert result.source_runs[0]["status"] == "collected"
        assert [int(m["current_rank"]) for m in result.source_mentions] == list(range(1, 21))
        for mention in result.source_mentions:
            assert mention["source_url"] == source["url"] and "/pd/" in mention["evidence_url"]
            assert mention["narrative_classification"]
            assert not ({"description", "review", "promotional_prose", "sales_units"} & mention.keys())


def test_prompt_is_opt_in_and_does_not_weaken_audiobooks_com() -> None:
    prompt = agent_extraction_prompt(sources()[0], {"html": "chart"}, 1000)
    assert "exact ranks 1 through 20" in prompt
    assert "narrative_classification" in prompt
    assert 'content_description"' not in prompt
    ordinary = agent_extraction_prompt(
        {
            "family": "audiobook ranking",
            "url": "https://www.audiobooks.com/browse/non-fiction",
            "language": "en",
        },
        {"html": "x"},
        1000,
    )
    assert "Do not invent numeric ranks" in ordinary


def test_configured_response_limit_and_oversize_fail_closed() -> None:
    class Response:
        def __init__(self, size: int):
            self.body = b"x" * size

        def read(self, limit: int) -> bytes:
            return self.body[:limit]

        def getheader(self, _name: str, default: str = "") -> str:
            return default

    assert len(_bounded_response_text(Response(1_050_000), 1_100_000)[0]) == 1_050_000
    with pytest.raises(CollectorRunError, match="response-size limit"):
        _bounded_response_text(Response(1_100_001), 1_100_000)


def test_config_rejects_query_pagination_for_explicit_chart() -> None:
    errors = validate_config(
        {
            "run_date": "2026-07-19",
            "sources": [{"name": "bad", "url": "https://audible.com/charts?pg=2", "audible_explicit_chart": True}],
        }
    )
    assert any("query" in e for e in errors)


def _mention(title: str, author: str, source_name: str, family: str, classification: str) -> dict[str, Any]:
    retailer = family == "audiobook ranking"
    return {
        "title": title,
        "primary_authors": [author],
        "source_name": source_name,
        "source_family": family,
        "source_url": "https://example.test/list",
        "evidence_url": "https://example.test/book",
        "source_check_date": "2026-07-19",
        "signal_type": family,
        "signal_strength": "top ranking",
        "evidence_note": "Rank 1.",
        "narrative_classification": classification,
        "retailer_discovery": retailer,
    }


def test_retailer_only_memoir_is_suppressed_but_mixed_source_attaches() -> None:
    audible = _mention("Kitchen Confidential", "Anthony Bourdain", NAMES[1], "audiobook ranking", "memoir")
    candidates, exclusions = build_scouting_candidates([audible], "2026-07-19")
    assert candidates == []
    assert exclusions[0]["exclusion_reason"] == "retailer-only memoir/autobiography/personal-life narrative"
    independent = _mention(
        "Kitchen Confidential", "Anthony Bourdain", "NPR Books", "media/curated list", "reported nonfiction"
    )
    candidates, _ = build_scouting_candidates([audible, independent], "2026-07-19")
    assert len(candidates) == 1 and len(candidates[0].to_dict()["source_evidence"]) == 2


def test_audiobooks_only_stays_eligible_but_does_not_rescue_audible_memoir() -> None:
    audiobooks = _mention(
        "Kitchen Confidential",
        "Anthony Bourdain",
        "Audiobooks.com Nonfiction Audiobook Bestsellers",
        "audiobook ranking",
        "memoir",
    )
    audiobooks["retailer_discovery"] = False
    candidates, _ = build_scouting_candidates([audiobooks], "2026-07-19")
    assert len(candidates) == 1
    audible = _mention("Kitchen Confidential", "Anthony Bourdain", NAMES[1], "audiobook ranking", "memoir")
    candidates, exclusions = build_scouting_candidates([audible, audiobooks], "2026-07-19")
    assert not candidates
    assert exclusions[0]["exclusion_reason"] == "retailer-only memoir/autobiography/personal-life narrative"


def test_non_memoir_and_third_person_biography_are_not_suppressed() -> None:
    mentions = [
        _mention("Science", "A Author", NAMES[0], "audiobook ranking", "reported nonfiction"),
        _mention("Biography", "B Author", NAMES[1], "audiobook ranking", "third-person biography"),
    ]
    candidates, _ = build_scouting_candidates(mentions, "2026-07-19")
    assert len(candidates) == 2


def test_rank_title_author_url_are_grounded_in_one_entry(tmp_path: Path) -> None:
    source = sources()[0]
    extraction = __import__("json").loads(Path(source["extraction_response_file"]).read_text())
    extraction["mentions"][0]["current_rank"], extraction["mentions"][1]["current_rank"] = 2, 1
    path = tmp_path / "swapped.json"
    path.write_text(__import__("json").dumps(extraction))
    result = SourceCollection(ROOT / "configs/current-weekly-pilot.json", tmp_path, "2026-07-19").collect(
        [{**source, "extraction_response_file": str(path)}]
    )
    # Both swapped rows are refused; since #839 the chart keeps its other rows.
    run = result.source_runs[0]
    assert run["status"] == "collected"
    assert run["valid_mention_count"] == len(extraction["mentions"]) - 2
    assert {drop["title"] for drop in run["drop_reasons"]} == {
        extraction["mentions"][0]["title"],
        extraction["mentions"][1]["title"],
    }
    page = __import__("json").loads(Path(source["source_page_fixture_file"]).read_text())
    swapped = extraction["mentions"][0]
    assert not audible_chart_mention_is_grounded(
        page["html"],
        source["url"],
        rank=swapped["current_rank"],
        title=swapped["title"],
        authors=swapped["primary_authors"],
        evidence_url=swapped["evidence_url"],
    )
    reason = extraction_contract_violation(swapped, swapped, source, page)
    assert "in one entry" in reason


def test_nested_author_li_stays_inside_enclosing_entry_context() -> None:
    """Nested <li> blocks (an Audible flyout) must not flush the pending
    product anchor: the "By: <author>" text they carry stays part of the
    enclosing product entry context, so rank/title/author/URL ground in one
    entry (Issue #385)."""
    html = (
        "<ol>"
        '<li class="chart-entry"><span class="rank">1</span>'
        '<a href="https://www.audible.com/pd/Sapiens-Audiobook/B0741G911Q">Sapiens</a>'
        '<ul class="flyout"><li>By: Yuval Noah Harari</li><li>Length: 16 hrs and 41 mins</li></ul>'
        "</li>"
        '<li class="chart-entry"><span class="rank">2</span>'
        '<a href="https://www.audible.com/pd/The-Body-Audiobook/0147526922">The Body</a>'
        '<ul class="flyout"><li>By: Bill Bryson</li></ul>'
        "</li>"
        "</ol>"
    )
    assert audible_chart_mention_is_grounded(
        html,
        "https://www.audible.com/charts",
        rank=1,
        title="Sapiens",
        authors=["Yuval Noah Harari"],
        evidence_url="https://www.audible.com/pd/Sapiens-Audiobook/B0741G911Q",
    )
    assert audible_chart_mention_is_grounded(
        html,
        "https://www.audible.com/charts",
        rank=2,
        title="The Body",
        authors=["Bill Bryson"],
        evidence_url="https://www.audible.com/pd/The-Body-Audiobook/0147526922",
    )
    # The nested flyout must not leak the next sibling entry's author into
    # this entry's context.
    assert not audible_chart_mention_is_grounded(
        html,
        "https://www.audible.com/charts",
        rank=1,
        title="Sapiens",
        authors=["Bill Bryson"],
        evidence_url="https://www.audible.com/pd/Sapiens-Audiobook/B0741G911Q",
    )


def test_inline_formatted_product_title_keeps_its_punctuation() -> None:
    url = "https://www.audible.com/pd/Dune-Book-1/B000000001"
    html = (
        '<ol><li class="chart-entry"><span class="rank">1</span>'
        f'<a href="{url}"><h3><b>Dune</b>, Book 1</h3></a>'
        '<span class="author">By: Frank Herbert</span></li></ol>'
    )
    assert audible_chart_mention_is_grounded(
        html,
        "https://www.audible.com/charts",
        rank=1,
        title="Dune, Book 1",
        authors=["Frank Herbert"],
        evidence_url=url,
    )


def test_nested_product_entry_never_grounds_the_enclosing_entry() -> None:
    """A nested <li> that carries its own product anchor is a different
    product, so its author must not ground a claim about the enclosing one.

    Admitting nested blocks into the entry context (so a flyout author counts)
    must not widen the context to a nested *product*. Both directions matter:
    the nested product can appear before or after the enclosing product's own
    author text."""
    html = (
        "<ol>"
        '<li class="chart-entry"><span class="rank">1</span>'
        '<a href="https://www.audible.com/pd/Sapiens-Audiobook/B0741G911Q">Sapiens</a>'
        '<ol><li class="chart-entry"><span class="rank">2</span>'
        '<a href="https://www.audible.com/pd/The-Body-Audiobook/0147526922">The Body</a>'
        "<ul><li>By: Bill Bryson</li></ul>"
        "</li></ol>"
        "<ul><li>By: Yuval Noah Harari</li></ul>"
        "</li>"
        "</ol>"
    )
    # Each product still grounds against its own author.
    assert audible_chart_mention_is_grounded(
        html,
        "https://www.audible.com/charts",
        rank=1,
        title="Sapiens",
        authors=["Yuval Noah Harari"],
        evidence_url="https://www.audible.com/pd/Sapiens-Audiobook/B0741G911Q",
    )
    assert audible_chart_mention_is_grounded(
        html,
        "https://www.audible.com/charts",
        rank=2,
        title="The Body",
        authors=["Bill Bryson"],
        evidence_url="https://www.audible.com/pd/The-Body-Audiobook/0147526922",
    )
    # The nested product's author must not ground the enclosing product.
    assert not audible_chart_mention_is_grounded(
        html,
        "https://www.audible.com/charts",
        rank=1,
        title="Sapiens",
        authors=["Bill Bryson"],
        evidence_url="https://www.audible.com/pd/Sapiens-Audiobook/B0741G911Q",
    )
    # And the enclosing product's author must not ground the nested one.
    assert not audible_chart_mention_is_grounded(
        html,
        "https://www.audible.com/charts",
        rank=2,
        title="The Body",
        authors=["Yuval Noah Harari"],
        evidence_url="https://www.audible.com/pd/The-Body-Audiobook/0147526922",
    )


@pytest.mark.parametrize("mutation", ["missing", "duplicate"])
def test_page_requires_exact_ranks_once_each(tmp_path: Path, mutation: str) -> None:
    source = sources()[0]
    page = __import__("json").loads(Path(source["source_page_fixture_file"]).read_text())
    if mutation == "missing":
        page["html"] = page["html"].replace('<span class="rank">20</span>', '<span class="rank">21</span>')
    else:
        page["html"] = page["html"].replace('<span class="rank">20</span>', '<span class="rank">19</span>')
    path = tmp_path / "page.json"
    path.write_text(__import__("json").dumps(page))
    result = SourceCollection(ROOT / "configs/current-weekly-pilot.json", tmp_path, "2026-07-19").collect(
        [{**source, "source_page_fixture_file": str(path)}]
    )
    assert result.source_runs[0]["status"] == "failed"
    assert result.source_runs[0]["debug_metadata"]["safe_failure_status"] == "page_shape"


def test_retailer_marker_is_code_derived() -> None:
    source = sources()[0]
    raw = dict(__import__("json").loads(Path(source["extraction_response_file"]).read_text())["mentions"][0])
    assert "retailer_discovery" not in raw
    assert normalized_agent_mention(raw, source, "2026-07-19")["retailer_discovery"] is True


def test_closed_classification_and_model_controlled_retailer_marker_fail_closed(tmp_path: Path) -> None:
    source = sources()[0]
    data = __import__("json").loads(Path(source["extraction_response_file"]).read_text())
    data["mentions"][0]["narrative_classification"] = "personal-ish"
    data["mentions"][1]["retailer_discovery"] = False
    path = tmp_path / "bad.json"
    path.write_text(__import__("json").dumps(data))
    result = SourceCollection(ROOT / "configs/current-weekly-pilot.json", tmp_path, "2026-07-19").collect(
        [{**source, "extraction_response_file": str(path)}]
    )
    # The open classification refuses its row; since #839 the chart keeps its
    # other rows. The model's retailer marker is replaced by the code's own.
    run = result.source_runs[0]
    assert run["status"] == "collected"
    assert [drop["title"] for drop in run["drop_reasons"]] == [data["mentions"][0]["title"]]
    assert "closed vocabulary" in run["drop_reasons"][0]["reason"]
    kept = next(m for m in result.source_mentions if m["title"] == data["mentions"][1]["title"])
    assert kept["retailer_discovery"] is True


def test_cross_category_duplicate_preserves_evidence_but_one_family_contribution() -> None:
    collected = []
    for source in (sources()[0], sources()[2]):
        collected.extend(
            SourceCollection(ROOT / "configs/current-weekly-pilot.json", ROOT, "2026-07-19")
            .collect([source])
            .source_mentions
        )
    candidates, _ = build_scouting_candidates(collected, "2026-07-19")
    co = next(candidate.to_dict() for candidate in candidates if candidate.to_dict()["title"] == "Co-Intelligence")
    assert len(co["source_evidence"]) == 2
    assert co["independent_signal_count"] == 1
    assert co["independent_signal_families"] == ["audiobook ranking"]
    mixed = [
        *collected,
        _mention("Co-Intelligence", "Ethan Mollick", "NPR Books", "media/curated list", "reported nonfiction"),
    ]
    candidates, _ = build_scouting_candidates(mixed, "2026-07-19")
    co = next(candidate.to_dict() for candidate in candidates if candidate.to_dict()["title"] == "Co-Intelligence")
    assert co["independent_signal_count"] == 2
    assert len(co["source_evidence"]) == 3


def test_contradictory_audible_classifications_cannot_bypass_memoir_suppression() -> None:
    mentions = [
        _mention("Kitchen Confidential", "Anthony Bourdain", NAMES[0], "audiobook ranking", "memoir"),
        _mention("Kitchen Confidential", "Anthony Bourdain", NAMES[1], "audiobook ranking", "other nonfiction"),
    ]
    candidates, exclusions = build_scouting_candidates(mentions, "2026-07-19")
    assert not candidates
    assert exclusions[0]["exclusion_reason"] == "retailer-only memoir/autobiography/personal-life narrative"


def test_feasibility_policy_allows_only_head_405_501_and_valid_plain_get() -> None:
    source = sources()[0]
    page = __import__("json").loads(Path(source["source_page_fixture_file"]).read_text())
    normal = evaluate_audible_chart_feasibility(
        source, head_status=200, get_status=200, resolved_url=source["url"], html=page["html"]
    )
    assert normal.accepted and normal.use_get_fallback is False
    for status in (405, 501):
        fallback = evaluate_audible_chart_feasibility(
            source, head_status=status, get_status=200, resolved_url=source["url"], html=page["html"]
        )
        assert fallback.accepted and fallback.use_get_fallback is True
    for status in (403, 429, 500, 502):
        assert not evaluate_audible_chart_feasibility(
            source, head_status=status, get_status=None, resolved_url=source["url"], html=""
        ).accepted
    assert not evaluate_audible_chart_feasibility(
        source,
        head_status=405,
        get_status=200,
        resolved_url=source["url"],
        html="<h1>CAPTCHA</h1> verify you are human",
    ).accepted
    assert not evaluate_audible_chart_feasibility(
        source, head_status=405, get_status=200, resolved_url=source["url"] + "?page=2", html=page["html"]
    ).accepted
    missing_author = page["html"].replace("By: Yuval Noah Harari", "By: ")
    verdict = evaluate_audible_chart_feasibility(
        source, head_status=200, get_status=200, resolved_url=source["url"], html=missing_author
    )
    assert not verdict.accepted and "primary author" in verdict.reason


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"underlying_work_type": "podcast"}, "underlying_work_type"),
        ({"publication_status": "forthcoming"}, "publication_status"),
        ({"candidate_category": ""}, "candidate_category"),
        ({"evidence_note": ""}, "evidence_note"),
        ({"evidence_note": "Print sales of 10,000 copies."}, "evidence_note"),
        ({"source_check_date": "2026-01-01"}, "prohibited fields"),
        ({"retailer_discovery": False}, "prohibited fields"),
        ({"description": "copied prose"}, "prohibited fields"),
    ],
)
def test_audible_raw_contract_rejects_ineligible_or_unsafe_fields(override: dict[str, Any], reason: str) -> None:
    source = sources()[0]
    page = __import__("json").loads(Path(source["source_page_fixture_file"]).read_text())
    mention = dict(__import__("json").loads(Path(source["extraction_response_file"]).read_text())["mentions"][0])
    mention.update(override)
    assert reason in extraction_contract_violation(mention, mention, source, page)


def test_live_caps_are_sufficient_for_complete_chart_output() -> None:
    config = load_validated_config(ROOT / "configs/current-weekly-live.json")
    selected = [source for source in config["sources"] if source["name"] in NAMES]
    assert all(source["max_model_input_chars"] >= 1_200_000 for source in selected)
    assert all(source["model_max_output_tokens"] >= 8_000 for source in selected)
    for source in sources():
        page = __import__("json").loads(Path(source["source_page_fixture_file"]).read_text())
        model_page, debug = model_bounded_page_data(page, 1_500_000)
        assert debug["model_input_truncated"] is False
        assert '<span class="rank">20</span>' in model_page["html"]
        prompt = agent_extraction_prompt(source, page, 1_500_000)
        assert '<span class="rank">20</span>' in prompt
        assert '"publisher"' not in prompt and '"publication_year"' not in prompt


def test_live_fetch_is_one_get_to_exact_base_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = sources()[0]
    page = __import__("json").loads(Path(source["source_page_fixture_file"]).read_text())
    seen: list[tuple[str, str]] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, limit: int) -> bytes:
            return page["html"].encode()[:limit]

        def geturl(self) -> str:
            return source["url"]

        def getheader(self, _name: str, default: str = "") -> str:
            return default

    def fake_urlopen(request: Any, **_kwargs: Any) -> Response:
        seen.append((request.get_method(), request.full_url))
        return Response()

    monkeypatch.setattr("autoscout.source_collectors.urlopen", fake_urlopen)
    fetched, _ = fetch_agent_source_page(tmp_path / "config.json", tmp_path, source)
    assert seen == [("GET", source["url"])]
    assert "?" not in seen[0][1] and '<span class="rank">20</span>' in fetched["html"]


def _healthy_sibling() -> dict[str, Any]:
    return {
        "name": "Healthy sibling",
        "family": "award",
        "url": "https://example.test/list",
        "active": True,
        "access_status": "open",
        "check_frequency": "weekly",
        "language": "en",
        "fixture_candidates": [
            {
                "title": "Healthy Book",
                "primary_authors": ["A Author"],
                "source_url": "https://example.test/book",
                "signal_type": "award",
                "signal_strength": "winner",
                "evidence_note": "Winner.",
            }
        ],
    }


def test_blocked_audible_access_is_named_and_isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = dict(sources()[0])
    source.update(extraction_method="agent-extraction-live", enable_live=True, extraction_backend="real")
    source.pop("source_page_fixture_file", None)
    source.pop("extraction_response_file", None)

    def blocked(*_args: Any, **_kwargs: Any) -> Any:
        raise HTTPError(source["url"], 429, "blocked", {}, None)

    monkeypatch.setattr("autoscout.source_collectors.urlopen", blocked)
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-07-19").collect([source, _healthy_sibling()])
    assert result.source_runs[0]["source_name"] == source["name"]
    assert result.source_runs[0]["status"] == "skipped"
    assert "429" in result.source_runs[0]["reason"]
    assert result.source_runs[1]["status"] == "collected"


def test_wrong_target_and_drift_fail_named_source_without_blocking_sibling(tmp_path: Path) -> None:
    source = sources()[0]
    page = __import__("json").loads(Path(source["source_page_fixture_file"]).read_text())
    page["resolved_url"] = "https://www.audible.com/charts/unrelated"
    page_path = tmp_path / "wrong.json"
    page_path.write_text(__import__("json").dumps(page))
    sibling = _healthy_sibling()
    result = SourceCollection(ROOT / "configs/current-weekly-pilot.json", tmp_path, "2026-07-19").collect(
        [{**source, "source_page_fixture_file": str(page_path)}, sibling]
    )
    assert result.source_runs[0]["source_name"] == source["name"]
    assert result.source_runs[0]["status"] == "failed"
    assert result.source_runs[1]["status"] == "collected"
    assert any(mention["source_name"] == "Healthy sibling" for mention in result.source_mentions)


def _live_shaped_chart_entry(*, rank: int, title: str, path: str, author: str) -> str:
    """One chart entry in the live markup, with the author in a sibling list item."""
    return (
        '<li class="bc-list-item productListItem"><span><ul class="bc-list bc-list-nostyle">'
        f'<li class="bc-list-item"><h3><span class="bc-text"> {rank}. </span>'
        f'<a class="bc-link" href="{path}">{title}</a></h3></li>'
        '<li class="bc-list-item authorLabel"><span class="bc-text"> By: '
        f'<a class="bc-link" href="/author/{author.replace(" ", "-")}/B000">{author}</a></span></li>'
        "</ul></span></li>"
    )


@pytest.mark.parametrize("author", ["David Byrne", "Robert Parker", "Kurt Vonnegut"])
def test_author_whose_name_starts_like_a_by_marker_is_grounded(author: str) -> None:
    """The 2026-11-03 run dropped "Sleeping Beauties" by David Byrne: splitting the
    product container on the "By:" marker also split "Byrne", so the name never matched."""
    path = "/pd/Sleeping-Beauties-Audiobook/B0GXCXZ5PB"
    html = (
        '<ul class="bc-list">'
        + _live_shaped_chart_entry(rank=15, title="Sleeping Beauties", path=path, author=author)
        + "</ul>"
    )
    grounded = partial(
        audible_chart_mention_is_grounded,
        html,
        "https://www.audible.com/charts/best/science-engineering-audiobooks/18580540011",
        rank=15,
        title="Sleeping Beauties",
        evidence_url="https://www.audible.com" + path,
    )
    assert grounded(authors=[author])
    assert not grounded(authors=["David Bowie"])
