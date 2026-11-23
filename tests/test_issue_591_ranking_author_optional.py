"""Offline tests for issue #591, an author-optional ranking row.

The markup is synthetic first-party-shaped HTML.  It is not live evidence and
it does not claim that any source has passed a live probe or an onboarding
confirmation gate.  No test here makes a network request.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autoscout.helper_metadata import HelperMetadataError
from autoscout.helper_metadata import enrich_with_helper_metadata
from autoscout.helper_metadata import load_helper_metadata
from autoscout.ranking_author_policy import mention_author_exempt
from autoscout.ranking_author_policy import ranking_author_optional_declared
from autoscout.ranking_author_policy import ranking_row_author_required
from autoscout.replay_fixtures import ReplayFixtureError
from autoscout.replay_fixtures import build_replay_fixture_pair
from autoscout.source_collection import SourceCollection
from autoscout.source_feasibility import OUTCOME_ACCEPT
from autoscout.source_feasibility import OUTCOME_REJECT
from autoscout.source_feasibility import evaluate_source_feasibility
from autoscout.source_mentions import validate_source_mention
from autoscout.source_page_identity import ranking_mention_is_grounded
from autoscout.source_page_identity import validate_ranking_contract_metadata

URL = "https://example.test/mas-vendidos/"
HEADING = "Los más vendidos de la semana"

BASE_METADATA: dict[str, Any] = {
    "contract_family": "ranking-table",
    "metadata_ranking_table": True,
    "ranking_row_selector": ".ranked-row",
    "ranking_rank_source": "document_order",
    "ranking_title_selector": ".row-title",
    "ranking_author_selector": ".row-author",
    "ranking_identifier_selector": ".row-isbn",
    "ranking_category_marker": "más vendidos",
    "ranking_reporting_period_undated": True,
    "ranking_expected_raw_row_count": 2,
    "ranking_evidence_url": "source_page",
    "adult_nonfiction_exclusions": ["libro de actividades"],
    "visible_title_selector": ".page-heading",
    "expected_page_heading": HEADING,
    "expected_signal_strength": "top ranking",
    "eligible_candidate_category": "adult nonfiction",
    "language": "es",
}


def _row(title: str, author: str, isbn: str) -> str:
    """Render one row; an empty author renders an empty declared element."""
    return (
        '<div class="ranked-row">'
        f'<div class="row-title">{title}</div>'
        f'<div class="row-author">{author}</div>'
        f'<span class="row-isbn" hidden>{isbn}</span>'
        "</div>"
    )


def _html(rows: str) -> str:
    return (
        "<!doctype html><html><head><title>Stale title</title></head><body>"
        f'<div class="page-heading">{HEADING}</div>{rows}</body></html>'
    )


_AUTHORED = _row("HISTORIA ARGENTINA", "AUTORA UNA", "9780306406157")
_AUTHORLESS = _row("CODIGO ANONIMO", "", "9780140328721")


def _source(**overrides: Any) -> dict[str, Any]:
    return {"name": "Synthetic ranking", "family": "bestseller/ranking", "url": URL, **BASE_METADATA, **overrides}


def _evidence(html: str) -> dict[str, Any]:
    return {
        "collection_mode": "source-candidate-probe",
        "source_url": URL,
        "source_page": URL,
        "resolved_url": URL,
        "head_status": 200,
        "get_status": 200,
        "reference_date": "2026-08-24",
        "html": html,
    }


# --- Declaration -----------------------------------------------------------


def test_declaration_is_accepted_by_the_contract_validator() -> None:
    assert validate_ranking_contract_metadata(_source(ranking_author_optional=True)) == ""
    assert ranking_author_optional_declared(_source(ranking_author_optional=True)) is True


def test_author_stays_required_without_the_declaration() -> None:
    assert ranking_row_author_required(_source()) is True
    assert ranking_author_optional_declared(_source()) is False


def test_declaration_must_be_a_boolean() -> None:
    error = validate_ranking_contract_metadata(_source(ranking_author_optional="yes"))
    assert error == "ranking_author_optional must be true or false"


def test_declaration_requires_the_author_selector() -> None:
    source = _source(ranking_author_optional=True)
    source["ranking_author_selector"] = ""
    error = validate_ranking_contract_metadata(source)
    assert error == "ranking_author_optional requires ranking_author_selector"


def test_a_declared_false_flag_keeps_the_author_requirement() -> None:
    assert ranking_row_author_required(_source(ranking_author_optional=False)) is True


# --- Feasibility row shape -------------------------------------------------


def test_an_authorless_row_rejects_the_page_without_the_declaration() -> None:
    verdict = evaluate_source_feasibility(_source(), _evidence(_html(_AUTHORED + _AUTHORLESS)))
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason == "ranking row missing author"


def test_an_authorless_row_is_accepted_with_the_declaration() -> None:
    verdict = evaluate_source_feasibility(
        _source(ranking_author_optional=True), _evidence(_html(_AUTHORED + _AUTHORLESS))
    )
    assert verdict.outcome == OUTCOME_ACCEPT


def test_the_declaration_does_not_excuse_a_missing_title() -> None:
    rows = _AUTHORED + _row("", "", "9780140328721")
    verdict = evaluate_source_feasibility(_source(ranking_author_optional=True), _evidence(_html(rows)))
    assert verdict.outcome == OUTCOME_REJECT
    assert "title" in verdict.reason


def test_the_declaration_does_not_excuse_a_missing_identifier() -> None:
    rows = (
        _AUTHORED + '<div class="ranked-row"><div class="row-title">SIN ISBN</div><div class="row-author"></div></div>'
    )
    verdict = evaluate_source_feasibility(_source(ranking_author_optional=True), _evidence(_html(rows)))
    assert verdict.outcome == OUTCOME_REJECT
    assert "identifier" in verdict.reason


# --- Grounding -------------------------------------------------------------


def _grounded(html: str, *, authors: list[str], title: str, isbn: str, rank: str, source: dict[str, Any]) -> bool:
    return ranking_mention_is_grounded(
        html,
        title=title,
        primary_authors=authors,
        isbn=isbn,
        current_rank=rank,
        source_week="2026-08-24",
        entry_scope_selector="",
        ranking_source=source,
    )


def test_an_authorless_mention_grounds_against_its_authorless_row() -> None:
    source = _source(ranking_author_optional=True)
    assert _grounded(
        _html(_AUTHORED + _AUTHORLESS),
        authors=[],
        title="CODIGO ANONIMO",
        isbn="9780140328721",
        rank="2",
        source=source,
    )


def test_an_authorless_mention_never_grounds_against_an_authored_row() -> None:
    source = _source(ranking_author_optional=True)
    assert not _grounded(
        _html(_AUTHORED + _AUTHORLESS),
        authors=[],
        title="HISTORIA ARGENTINA",
        isbn="9780306406157",
        rank="1",
        source=source,
    )


def test_an_authored_row_still_requires_its_exact_author() -> None:
    source = _source(ranking_author_optional=True)
    assert not _grounded(
        _html(_AUTHORED + _AUTHORLESS),
        authors=["AUTOR EQUIVOCADO"],
        title="HISTORIA ARGENTINA",
        isbn="9780306406157",
        rank="1",
        source=source,
    )
    assert _grounded(
        _html(_AUTHORED + _AUTHORLESS),
        authors=["AUTORA UNA"],
        title="HISTORIA ARGENTINA",
        isbn="9780306406157",
        rank="1",
        source=source,
    )


def test_an_invented_author_never_grounds_against_an_authorless_row() -> None:
    source = _source(ranking_author_optional=True)
    assert not _grounded(
        _html(_AUTHORED + _AUTHORLESS),
        authors=["AUTOR INVENTADO"],
        title="CODIGO ANONIMO",
        isbn="9780140328721",
        rank="2",
        source=source,
    )


# --- Replay fixture generation ---------------------------------------------


def test_replay_fixture_emits_authorless_primary_authors_and_omits_author_note() -> None:
    source = _source(ranking_author_optional=True)
    evidence = _evidence(_html(_AUTHORED + _AUTHORLESS))
    evidence.update(
        {
            "source_name": "Synthetic ranking",
            "signal_family": "bestseller/ranking",
            "contract_family": "ranking-table",
            "contract_metadata": source,
        }
    )
    helper = {
        "version": 1,
        "source_url": URL,
        "language": "es",
        "entries": [
            {
                "title": "HISTORIA ARGENTINA",
                "author": "AUTORA UNA",
                "evidence_url": URL,
                "english_title": "Argentine History",
                "search_translation": "История Аргентины",
                "author_transliteration_note": "Автора Уна",
            },
            {
                "title": "CODIGO ANONIMO",
                "author": "",
                "evidence_url": URL,
                "english_title": "Anonymous Code",
                "search_translation": "Анонимный кодекс",
            },
        ],
    }

    pair = build_replay_fixture_pair(
        URL,
        "authorless-ranking",
        evidence,
        signal_strength="top ranking",
        candidate_category="adult nonfiction",
        language="es",
        helper_metadata=helper,
    )

    mention = next(item for item in pair.extraction["mentions"] if item["title"] == "CODIGO ANONIMO")
    assert mention["primary_authors"] == []
    assert "author_transliteration_note" not in mention
    assert ranking_mention_is_grounded(
        pair.page["html"],
        title=mention["title"],
        isbn=mention["isbn"],
        current_rank=mention["current_rank"],
        source_week=mention["source_week"],
        primary_authors=mention["primary_authors"],
        ranking_source=source,
        evidence_url=mention["evidence_url"],
    )

    without_declaration = {**evidence, "contract_metadata": _source()}
    with pytest.raises(ReplayFixtureError, match="ranking row missing author"):
        build_replay_fixture_pair(
            URL,
            "authorless-ranking",
            without_declaration,
            signal_strength="top ranking",
            candidate_category="adult nonfiction",
            language="es",
            helper_metadata=helper,
        )


# --- Mention contract ------------------------------------------------------


def _mention(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "title": "CODIGO ANONIMO",
        "source_url": URL,
        "source_check_date": "2026-08-24",
        "signal_type": "bestseller/ranking",
        "signal_strength": "top ranking",
        "evidence_note": "Bounded ranking row on the captured source page.",
    }
    value.update(overrides)
    return value


def test_the_exemption_is_limited_to_a_declaring_ranking_source() -> None:
    assert mention_author_exempt(_source(ranking_author_optional=True)) is True
    assert mention_author_exempt(_source()) is False
    assert mention_author_exempt({"contract_family": "exact-book-list", "ranking_author_optional": True}) is False


def test_a_mention_without_authors_is_rejected_without_the_declaration() -> None:
    with pytest.raises(ValueError, match="primary_authors"):
        validate_source_mention(_source(), _mention(), context="source mention")


def test_a_mention_without_authors_is_accepted_with_the_declaration() -> None:
    validate_source_mention(_source(ranking_author_optional=True), _mention(), context="source mention")


def test_the_declaration_does_not_excuse_another_missing_mention_field() -> None:
    mention = _mention()
    mention.pop("evidence_note")
    with pytest.raises(ValueError, match="evidence_note"):
        validate_source_mention(_source(ranking_author_optional=True), mention, context="source mention")


# --- Collection ------------------------------------------------------------


def _replay_source(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    page_path = tmp_path / "page.json"
    extraction_path = tmp_path / "extraction.json"
    page_path.write_text(
        json.dumps(
            {
                "source_page": URL,
                "resolved_url": URL,
                "reference_date": "2026-08-24",
                "html": _html(_AUTHORED + _AUTHORLESS),
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    extraction_path.write_text(
        json.dumps(
            {
                "source_page": URL,
                "mentions": [
                    {
                        "title": "CODIGO ANONIMO",
                        "isbn": "9780140328721",
                        "current_rank": "2",
                        "source_week": "2026-08-24",
                        "source_url": URL,
                        "evidence_url": URL,
                        "signal_type": "bestseller/ranking",
                        "signal_strength": "top ranking",
                        "candidate_category": "adult nonfiction",
                        "evidence_note": "Bounded ranking row on the captured source page.",
                        "english_title": "Anonymous Code",
                        "search_translation": "Анонимный кодекс",
                        "author_transliteration_note": "",
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return _source(
        source_page_fixture_file=str(page_path),
        extraction_response_file=str(extraction_path),
        extraction_method="codex-extraction-replay",
        **overrides,
    )


def test_collection_publishes_an_authorless_mention_with_no_invented_author(tmp_path: Path) -> None:
    from autoscout.ranking_freshness import RankingFreshnessLedger

    source = _replay_source(tmp_path, ranking_author_optional=True)
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-08-24", RankingFreshnessLedger()).collect(
        [source]
    )
    assert result.source_runs[0]["status"] == "collected"
    assert len(result.source_mentions) == 1
    assert result.source_mentions[0]["primary_authors"] == []


def test_collection_drops_an_authorless_mention_without_the_declaration(tmp_path: Path) -> None:
    from autoscout.ranking_freshness import RankingFreshnessLedger

    source = _replay_source(tmp_path)
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-08-24", RankingFreshnessLedger()).collect(
        [source]
    )
    assert result.source_runs[0]["status"] == "failed"
    assert result.source_mentions == []


def test_the_helper_note_exemption_is_limited_to_an_authorless_mention() -> None:
    """A non-English mention that has an author still needs its author note."""
    from autoscout.helper_metadata import validate_helper_metadata

    entry = {
        "title": "CODIGO ANONIMO",
        "english_title": "Anonymous Code",
        "search_translation": "Анонимный кодекс",
    }
    assert "author_transliteration_note" in validate_helper_metadata(entry, title_relation=None)
    assert validate_helper_metadata(entry, title_relation=None, author_note_required=False) == ""


@pytest.mark.parametrize("author_field", ["primary_authors", "authors"])
def test_authorless_sidecar_enriches_explicit_empty_author_list(tmp_path: Path, author_field: str) -> None:
    sidecar = tmp_path / "helper.json"
    sidecar.write_text(
        json.dumps(
            {
                "version": 1,
                "source_url": URL,
                "language": "es",
                "entries": [
                    {
                        "title": "CODIGO ANONIMO",
                        "author": "",
                        "evidence_url": URL,
                        "english_title": "Anonymous Code",
                        "search_translation": "Анонимный кодекс",
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    index = load_helper_metadata(
        tmp_path / "config.json",
        _source(ranking_author_optional=True, helper_metadata_file=str(sidecar)),
    )
    assert index is not None

    enriched, matched = enrich_with_helper_metadata(
        {"title": "CODIGO ANONIMO", "evidence_url": URL, author_field: []}, index
    )

    assert matched == ("CODIGO ANONIMO", "", URL)
    assert enriched["english_title"] == "Anonymous Code"
    assert enriched["search_translation"] == "Анонимный кодекс"
    assert "author_transliteration_note" not in enriched

    missing_author, missing_match = enrich_with_helper_metadata({"title": "CODIGO ANONIMO", "evidence_url": URL}, index)
    assert missing_match is None
    assert missing_author == {"title": "CODIGO ANONIMO", "evidence_url": URL}


def test_non_author_optional_sidecar_rejects_blank_author_identity(tmp_path: Path) -> None:
    sidecar = tmp_path / "helper.json"
    sidecar.write_text(
        json.dumps(
            {
                "version": 1,
                "source_url": URL,
                "language": "es",
                "entries": [
                    {
                        "title": "CODIGO ANONIMO",
                        "author": "",
                        "evidence_url": URL,
                        "english_title": "Anonymous Code",
                        "search_translation": "Анонимный кодекс",
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(HelperMetadataError, match="blank identity field"):
        load_helper_metadata(tmp_path / "config.json", _source(helper_metadata_file=str(sidecar)))


def test_an_authored_mention_from_a_declaring_source_still_needs_its_author_note(tmp_path: Path) -> None:
    from autoscout.source_collectors.base import extraction_contract_violation

    source = _source(ranking_author_optional=True)
    raw_entry = {
        "title": "HISTORIA ARGENTINA",
        "primary_authors": ["AUTORA UNA"],
        "isbn": "9780306406157",
        "current_rank": "1",
        "source_week": "2026-08-24",
        "source_url": URL,
        "evidence_url": URL,
        "signal_type": "bestseller/ranking",
        "signal_strength": "top ranking",
        "candidate_category": "adult nonfiction",
        "evidence_note": "Bounded ranking row on the captured source page.",
        "english_title": "Argentine History",
        "search_translation": "История Аргентины",
        "author_transliteration_note": "",
    }
    violation = extraction_contract_violation(
        raw_entry,
        {"signal_type": "bestseller/ranking"},
        source,
        {"html": _html(_AUTHORED + _AUTHORLESS)},
    )
    assert "author_transliteration_note" in violation
