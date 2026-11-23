"""Metadata-only captures cannot supply a Content Description (issue #868)."""

import json
from pathlib import Path

import pytest

from autoscout.codex_workflow import finalize_codex_run
from autoscout.codex_workflow import prepare_codex_extraction
from autoscout.config import load_validated_config
from autoscout.config import validate_config
from autoscout.content_description import DESCRIPTION_FALLBACK
from autoscout.content_description import record_has_usable_description
from autoscout.content_description import validate_content_description
from autoscout.pipeline import build_scouting_candidates
from autoscout.pipeline import update_candidate_history_snapshots
from autoscout.source_mentions import SourceMention

TEMPLATE = "Нон-фикшн о теме, обозначенной в названии; редакционный хук — позиция {rank} в рейтинге."
REAL_DESCRIPTION = (
    "Автор исследует, как государственная жилищная политика закрепляла расовую сегрегацию городов. "
    "Архивные документы показывают связь законов, банковского кредитования и неравенства."
)
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "text",
    [
        *(TEMPLATE.format(rank=rank) for rank in (14, 33, 37)),
        "Книга о теме, обозначенной в названии.",
        "Книга посвящена теме, указанной в названии.",
        "Тема книги отражена в названии.",
        "Позиция 14 в рейтинге.",
        "Книга занимает позицию 33 в рейтинге.",
        "Книга находится на 37-м месте в рейтинге.",
        "Книга на позиции 14 в рейтинге.",
        "Книга занимает 33-ю позицию в чарте.",
        "37-е место.",
        "Позиция № 14 в рейтинге Independent Press Top 40 Nonfiction.",
        "Книга занимает 37-е место в рейтинге Independent Press Top 40 Nonfiction.",
        "Нон-фикшн на 14-м месте в чарте.",
        "Редакционный хук — 33-е место в списке бестселлеров.",
        "  ПОЗИЦИЯ  37\nв рейтинге!  ",
    ],
)
def test_title_and_rank_only_templates_are_rejected(text):
    validated, reason = validate_content_description(text)
    assert validated is None
    assert reason == "contentless title/rank template rejected"


@pytest.mark.parametrize(
    "text",
    [
        REAL_DESCRIPTION,
        f"Позиция 37 в рейтинге. {REAL_DESCRIPTION}",
        f"{REAL_DESCRIPTION} Книга занимает позицию 14 в рейтинге.",
        "Книга на 33-м месте в рейтинге; автор исследует историю налоговой политики и её влияние на неравенство.",
        "Тема, обозначенная в названии, раскрывается через историю городского расселения и архивы банков.",
        "Автор изучает рейтинги университетов: позиция в рейтинге влияет на доступ к финансированию.",
        "Книга занимает 37-е место в рейтинге Independent Press Top 40 Nonfiction и исследует расовую сегрегацию.",
    ],
)
def test_substantive_description_with_incidental_template_words_passes(text):
    assert validate_content_description(text) == (text, None)


def _source(**extra):
    return {
        "name": "Metadata chart",
        "family": "bestseller/ranking",
        "url": "https://example.test/chart",
        "language": "en",
        **extra,
    }


def _mention(text):
    return {
        "title": "The Color of Law",
        "primary_authors": ["Richard Rothstein"],
        "source_url": "https://example.test/chart",
        "source_check_date": "2026-10-26",
        "signal_type": "bestseller/ranking",
        "signal_strength": "bestseller list",
        "evidence_note": "Rank 14 on the current chart.",
        "content_description": text,
    }


@pytest.mark.parametrize("route", ["collector", "recorded", "candidate"])
@pytest.mark.parametrize("text", [REAL_DESCRIPTION, TEMPLATE.format(rank=14)])
def test_metadata_only_description_is_cleared_before_aggregation_and_history(route, text):
    source = _source(metadata_only=True)
    raw = _mention(text)
    if route == "collector":
        mention = SourceMention.from_collector(source, raw, "2026-10-26").to_dict()
    elif route == "recorded":
        mention = SourceMention.from_recorded(source, raw).to_dict()
    else:
        mention = SourceMention.from_candidate(source, raw, "2026-10-26").to_dict()
    assert mention["content_description"] == ""
    assert mention["content_description_rejection_reason"] == "metadata-only source has no book prose"
    # Revalidation must retain the bounded reason without restoring the text.
    revalidated = SourceMention.from_recorded(source, mention).to_dict()
    assert revalidated["content_description"] == ""
    assert revalidated["content_description_rejection_reason"] == mention["content_description_rejection_reason"]
    candidates, exclusions = build_scouting_candidates([mention], "2026-10-26")
    assert not exclusions
    assert len(candidates) == 1
    assert candidates[0].editor_description() == DESCRIPTION_FALLBACK
    history = update_candidate_history_snapshots([], candidates, "2026-10-26")
    assert len(history) == 1
    assert not record_has_usable_description(history[0])
    assert "description_provenance" not in history[0]


@pytest.mark.parametrize("policy", [{}, {"metadata_only": False}])
def test_prose_source_still_supplies_description(policy):
    source = _source(**policy)
    mention = SourceMention.from_collector(source, _mention(REAL_DESCRIPTION), "2026-10-26").to_dict()
    assert mention["content_description"] == REAL_DESCRIPTION
    assert "content_description_rejection_reason" not in mention


def test_metadata_only_source_does_not_discard_another_sources_real_description():
    metadata = SourceMention.from_collector(
        _source(metadata_only=True), _mention(TEMPLATE.format(rank=14)), "2026-10-26"
    ).to_dict()
    prose = SourceMention.from_collector(_source(name="Review"), _mention(REAL_DESCRIPTION), "2026-10-26").to_dict()
    for mentions in ([metadata, prose], [prose, metadata]):
        candidates, _ = build_scouting_candidates(mentions, "2026-10-26")
        history = update_candidate_history_snapshots([], candidates, "2026-10-26")
        assert len(history) == 1
        assert history[0]["content_description"] == REAL_DESCRIPTION


@pytest.mark.parametrize("provenance", ["grounded", "editor-corrected"])
def test_metadata_only_intake_preserves_saved_descriptions(provenance):
    mention = SourceMention.from_collector(
        _source(metadata_only=True), _mention(REAL_DESCRIPTION), "2026-10-26"
    ).to_dict()
    candidates, _ = build_scouting_candidates([mention], "2026-10-26")
    existing = {
        "identity_key": candidates[0].identity_key(),
        "content_description": REAL_DESCRIPTION,
        "description_provenance": {"provenance": provenance, "content_description": REAL_DESCRIPTION},
    }
    history = update_candidate_history_snapshots([existing], candidates, "2026-10-26")
    assert history[0]["content_description"] == REAL_DESCRIPTION
    assert history[0]["description_provenance"] == existing["description_provenance"]


def test_template_from_a_prose_source_is_also_cleared_at_intake():
    mention = SourceMention.from_collector(_source(), _mention(TEMPLATE.format(rank=37)), "2026-10-26").to_dict()
    assert mention["content_description"] == ""
    assert mention["content_description_rejection_reason"] == "contentless title/rank template rejected"


def test_extraction_notes_are_not_a_runtime_policy():
    mention = SourceMention.from_collector(
        _source(extraction_notes="Metadata only. Never retain descriptions."),
        _mention(REAL_DESCRIPTION),
        "2026-10-26",
    ).to_dict()
    assert mention["content_description"] == REAL_DESCRIPTION


@pytest.mark.parametrize("metadata_only", [True, False])
def test_metadata_only_policy_survives_capture_finalize_replay(tmp_path, metadata_only):
    capture = tmp_path / "page.json"
    capture.write_text(json.dumps({"source_page": "https://example.test/chart", "html": "<h1>Chart</h1>"}))
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "run_date": "2026-10-26",
                "sources": [
                    _source(
                        metadata_only=metadata_only,
                        extraction_method="agent-extraction-live",
                        source_page_fixture_file=str(capture),
                    )
                ],
            }
        )
    )
    output = tmp_path / "output"
    assert prepare_codex_extraction(config, output) == 0
    item = json.loads((output / "codex-extraction-worklist.json").read_text())["work_items"][0]
    assert item["source_metadata"]["metadata_only"] is metadata_only
    (output / item["expected_extraction_file"]).write_text(json.dumps({"mentions": []}))
    assert finalize_codex_run(output) == 0
    replay_source = load_validated_config(output / "codex-replay-config.json")["sources"][0]
    assert replay_source["metadata_only"] is metadata_only
    mention = SourceMention.from_collector(replay_source, _mention(REAL_DESCRIPTION), "2026-10-26").to_dict()
    assert mention["content_description"] == ("" if metadata_only else REAL_DESCRIPTION)


@pytest.mark.parametrize("value", [True, False])
def test_metadata_only_is_a_valid_boolean_config_field(value):
    assert validate_config({"sources": [_source(metadata_only=value)]}) == []


@pytest.mark.parametrize("value", ["true", "false", 1, 0, None, []])
def test_metadata_only_config_rejects_non_booleans(value):
    errors = validate_config({"sources": [_source(metadata_only=value)]})
    assert len(errors) == 1
    assert "'metadata_only' must be bool" in errors[0]


def test_weekly_portfolio_declares_metadata_only_for_chart_captures_only():
    """Only charts are flagged: award, review and curated-list pages can carry book prose.

    A chart whose page prints a blurb per book (USA TODAY, Spiegel) may stay
    unflagged; the flag is never set on a non-chart source.
    """
    sources = json.loads((ROOT / "configs/current-weekly-portfolio.json").read_text())["sources"]
    for source in sources:
        if source.get("metadata_only") is True:
            assert source.get("family") in {"bestseller/ranking", "audiobook ranking"}, source["name"]
    resolved = load_validated_config(ROOT / "configs/current-weekly-pilot.json")
    ipc = next(source for source in resolved["sources"] if source["name"] == "Independent Press Top 40 Nonfiction")
    assert ipc["metadata_only"] is True
