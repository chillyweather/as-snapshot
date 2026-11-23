"""Offline PublishNews adapter tests for issues #560, #561, and #562.

The markup is synthetic first-party-shaped HTML derived from the retained
PublishNews issue facts.
The row field selectors match the bounded live diagnostics but remain test-only declarations.
It is not live evidence and it does not claim that PublishNews has passed a
live probe or an onboarding confirmation gate.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from autoscout.__main__ import main
from autoscout.config import validate_config
from autoscout.replay_fixtures import ReplayFixtureError
from autoscout.replay_fixtures import build_replay_fixture_pair
from autoscout.source_collection import SourceCollection
from autoscout.source_exit_codes import EXIT_CONFIRMATION
from autoscout.source_feasibility import evaluate_source_feasibility
from autoscout.source_page_identity import ranking_mention_is_grounded
from autoscout.source_page_identity import ranking_rows
from autoscout.source_page_identity import validate_page_shape
from autoscout.source_probe import probe_source_candidates

URL = "https://www.publishnews.com.br/ranking-nielsen/semanal/9"
HEADING = "Lista Nielsen-PublishNews de Mais Vendidos de Não Ficção Trade de 03/08/2026 a 09/08/2026"

# These row field selectors describe the bounded live diagnostics and the
# synthetic fixture. The test remains offline and does not claim onboarding.
RANKING_METADATA: dict[str, Any] = {
    "contract_family": "ranking-table",
    "ranking_row_selector": ".pn-ranking-livro",
    "ranking_rank_source": "document_order",
    "ranking_title_selector": ".pn-ranking-livro-nome",
    "ranking_author_selector": ".pn-ranking-livro-autor",
    "ranking_identifier_selector": ".pn-ranking-livro-isbn",
    "ranking_category_marker": "Não Ficção Trade",
    "ranking_reporting_period_pattern": r"(?P<period>\d{2}/\d{2}/\d{4} a \d{2}/\d{2}/\d{4})",
    "ranking_reporting_period_date_format": "%d/%m/%Y",
    "ranking_reporting_period_max_age_days": 14,
    "ranking_expected_raw_row_count": 20,
    "ranking_evidence_url": "source_page",
    "adult_nonfiction_exclusions": [
        "eu duvido você terminar este livro",
        "colorindo com aquarela",
        "livro de atividades",
    ],
    "visible_title_selector": ".pn-colunas-06x07 > .pn-colunas-corpo > .pn-editoria > .pn-editoria-cabeca > .pn-editoria-titulo",
}


def _row(rank: int, title: str, author: str, isbn: str, publisher: str = "") -> str:
    """Render a test-only first-party-shaped row without a rank element."""
    publisher_markup = f'<div class="pn-ranking-livro-editora">{publisher}</div>' if publisher else ""
    return (
        '<div class="pn-ranking-livro">'
        f'<div class="pn-ranking-livro-nome">{title}</div>'
        f'<div class="pn-ranking-livro-autor">{author}</div>'
        f'{publisher_markup}<span class="pn-ranking-livro-isbn" hidden>{isbn}</span>'
        "</div>"
    )


def _html(rows: str, *, heading: str = HEADING) -> str:
    return (
        "<!doctype html><html><head><title>Stale title</title></head><body>"
        f'<nav><a href="/advert">Advertisement</a></nav>'
        f'<div class="pn-colunas-06x07"><div class="pn-colunas-corpo"><div class="pn-editoria">'
        f'<div class="pn-editoria-cabeca"><div class="pn-editoria-titulo">{heading}</div></div>'
        f"</div></div></div>{rows}</body></html>"
    )


def _evidence(html: str, **overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "collection_mode": "source-candidate-probe",
        "source_url": URL,
        "source_page": URL,
        "resolved_url": URL,
        "head_status": 200,
        "get_status": 200,
        "reference_date": "2026-08-10",
        "html": html,
    }
    value.update(overrides)
    return value


def _pin_probe_date(monkeypatch: pytest.MonkeyPatch) -> None:
    class FixedProbeDate(date):
        @classmethod
        def today(cls) -> date:
            return cls(2026, 8, 10)

    monkeypatch.setattr("autoscout.source_probe.date", FixedProbeDate)


def _source(**overrides: Any) -> dict[str, Any]:
    return {
        "name": "PublishNews synthetic ranking",
        "family": "bestseller/ranking",
        "url": URL,
        "metadata_ranking_table": True,
        "expected_page_heading": HEADING,
        "expected_signal_strength": "top ranking",
        "eligible_candidate_category": "adult nonfiction",
        "language": "pt",
        **RANKING_METADATA,
        **overrides,
    }


def test_synthetic_test_only_field_selectors_accept_matching_rows() -> None:
    rows = _row(
        1,
        "Eu duvido você terminar este livro",
        "Giuliana Mafra",
        "9780306406157",
        "Outro Planeta",
    ) + _row(2, "Livro Adulto", "Autora Adulta", "9780140328721")
    verdict = evaluate_source_feasibility(_source(ranking_expected_raw_row_count=2), _evidence(_html(rows)))
    assert verdict.accepted
    assert verdict.contract_family == "ranking-table"
    assert verdict.ranking_category == "Não Ficção Trade"
    assert verdict.reporting_period == "03/08/2026 a 09/08/2026"
    assert verdict.bounded_entry_count == 1
    sample = verdict.samples[0]
    assert (sample.rank, sample.title, sample.author, sample.identifier, sample.url) == (
        "2",
        "Livro Adulto",
        "Autora Adulta",
        "9780140328721",
        URL,
    )


def test_document_order_assigns_original_positions_and_grounding_rejects_wrong_rank() -> None:
    source = _source(ranking_expected_raw_row_count=3)
    rows = (
        _row(1, "Eu duvido você terminar este livro", "Giuliana Mafra", "9780306406157")
        + _row(2, "Livro Dois", "Autora Dois", "9780140328721")
        + _row(3, "Livro Três", "Autora Três", "9781401971366")
    )
    evidence = _evidence(_html(rows))
    verdict = evaluate_source_feasibility(source, evidence)
    assert verdict.accepted
    assert [(sample.title, sample.rank) for sample in verdict.samples] == [("Livro Dois", "2"), ("Livro Três", "3")]

    sample = verdict.samples[0]
    assert ranking_mention_is_grounded(
        evidence["html"],
        title=sample.title,
        primary_author=sample.author,
        isbn=sample.identifier,
        current_rank=sample.rank,
        source_week=sample.reporting_period,
        ranking_source=source,
    )
    assert not ranking_mention_is_grounded(
        evidence["html"],
        title=sample.title,
        primary_author=sample.author,
        isbn=sample.identifier,
        current_rank="1",
        source_week=sample.reporting_period,
        ranking_source=source,
    )


def test_selector_rank_source_still_uses_the_declared_row_field() -> None:
    source = _source(
        ranking_rank_source="selector",
        ranking_rank_selector=".pn-ranking-livro-posicao",
        ranking_expected_raw_row_count=1,
    )
    html = _html(
        '<div class="pn-ranking-livro">'
        '<div class="pn-ranking-livro-posicao">7</div>'
        '<div class="pn-ranking-livro-nome">Livro Adulto</div>'
        '<div class="pn-ranking-livro-autor">Autora Adulta</div>'
        '<span class="pn-ranking-livro-isbn" hidden>9780306406157</span></div>'
    )
    verdict = evaluate_source_feasibility(source, _evidence(html))
    assert verdict.accepted
    assert verdict.samples[0].rank == "7"


def test_ranking_page_shape_uses_dynamic_period_identity_and_pinned_reference_date() -> None:
    source = _source(ranking_expected_raw_row_count=1)
    cases = (
        (HEADING, "2026-08-10", True),
        (HEADING.replace("03/08/2026 a 09/08/2026", "10/08/2026 a 16/08/2026"), "2026-08-17", True),
        (HEADING, "2026-08-24", False),
        (HEADING.replace("03/08/2026 a 09/08/2026", "17/08/2026 a 23/08/2026"), "2026-08-17", False),
        (HEADING.replace("03/08/2026 a 09/08/2026", "10/08/2026 a 09/08/2026"), "2026-08-10", False),
        (HEADING.replace("03/08/2026 a 09/08/2026", "periodo inválido"), "2026-08-10", False),
        (HEADING.replace("Não Ficção Trade", "Ficção"), "2026-08-10", False),
    )
    for heading, run_date, accepted in cases:
        error = validate_page_shape(
            source,
            {
                "source_page": URL,
                "resolved_url": URL,
                "html": _html(_row(1, "Livro Adulto", "Autora Adulta", "9780306406157"), heading=heading),
            },
            reference_date=date.fromisoformat(run_date),
        )
        assert (error == "") is accepted, (heading, run_date, error)


def test_probe_rejection_stdout_exposes_bounded_visible_row_structure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_probe_date(monkeypatch)
    malformed_rows = (
        '<div class="pn-ranking-livro"><div class="actual-title">Livro Adulto</div>'
        '<div class="actual-author">Autora Adulta</div>'
        '<span class="pn-ranking-livro-isbn" hidden>9780306406157</span></div>'
        '<div class="pn-ranking-livro"><div class="actual-title">Outro Livro</div>'
        '<div class="actual-author">Outra Autora</div>'
        '<span class="pn-ranking-livro-isbn" hidden>9780140328721</span></div>'
    )

    class Response:
        status = 200

        def read(self, _limit: int) -> bytes:
            return _html(malformed_rows).encode()

        def geturl(self) -> str:
            return URL

        def close(self) -> None:
            return None

    metadata = {**RANKING_METADATA, "ranking_expected_raw_row_count": 2}
    code = probe_source_candidates(
        [URL],
        contract_families=["ranking-table"],
        contract_metadata=[metadata],
        ledger_path=tmp_path / "ledger.json",
        feasibility_dir=tmp_path / "drafts",
        opener=lambda _request, **_kwargs: Response(),
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 1, report
    assert report["candidates"][0]["contract_metadata"]["ranking_rank_source"] == "document_order"
    assert "ranking_diagnostics" in report["candidates"][0], report
    diagnostic = report["candidates"][0]["ranking_diagnostics"]
    assert "actual-title" in diagnostic["class_tokens"]
    assert "actual-author" in diagnostic["class_tokens"]
    assert "Livro Adulto" in diagnostic["visible_text"]
    assert "9780306406157" not in json.dumps(diagnostic)
    assert "pn-ranking-livro-isbn" not in json.dumps(diagnostic)
    assert len(diagnostic["visible_text"]) <= 160


def test_probe_rejects_unsupported_rank_source_before_network_or_storage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = probe_source_candidates(
        [URL],
        contract_families=["ranking-table"],
        contract_metadata=[{**RANKING_METADATA, "ranking_rank_source": "unsupported"}],
        ledger_path=tmp_path / "ledger.json",
        opener=lambda *_args, **_kwargs: pytest.fail("network must not be called"),
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert "ranking_rank_source" in report["error"]
    assert not (tmp_path / "ledger.json").exists()


def test_probe_retains_document_order_metadata_in_capture_draft_and_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_probe_date(monkeypatch)
    rows = _row(1, "Eu duvido você terminar este livro", "Giuliana Mafra", "9780306406157") + _row(
        2, "Livro Adulto", "Autora Adulta", "9780140328721"
    )
    metadata = {**RANKING_METADATA, "ranking_expected_raw_row_count": 2}

    class Response:
        status = 200

        def read(self, _limit: int) -> bytes:
            return _html(rows).encode()

        def geturl(self) -> str:
            return URL

        def close(self) -> None:
            return None

    captures = tmp_path / "captures"
    drafts = tmp_path / "drafts"
    ledger = tmp_path / "ledger.json"
    code = probe_source_candidates(
        [URL],
        contract_families=["ranking-table"],
        contract_metadata=[metadata],
        keep_captures=captures,
        ledger_path=ledger,
        feasibility_dir=drafts,
        opener=lambda _request, **_kwargs: Response(),
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 0, report
    assert report["candidates"][0]["contract_metadata"]["ranking_rank_source"] == "document_order"
    capture = json.loads(next(captures.glob("*.json")).read_text(encoding="utf-8"))
    assert capture["contract_metadata"]["ranking_rank_source"] == "document_order"
    draft = next(drafts.glob("*.md")).read_text(encoding="utf-8")
    assert "Ranking rank source: document_order" in draft
    ledger_entry = json.loads(ledger.read_text(encoding="utf-8"))["entries"][0]
    assert ledger_entry["contract_metadata"]["ranking_rank_source"] == "document_order"


def test_synthetic_test_only_identity_and_blocked_markup_fail_closed() -> None:
    rows = _row(1, "Livro Adulto", "Autora Adulta", "9780306406157")
    for html, expected in (
        (_html(rows, heading=HEADING.replace("Não Ficção Trade", "Ficção")), "ranking_page_identity"),
        (_html(rows, heading=HEADING.replace("09/08/2026", "09/08/2025")), "ranking_page_identity"),
        (_html(rows).replace("pn-editoria-titulo", "other-title"), "ranking_page_identity"),
        (_html(rows).replace("Livro Adulto", "Verify you are human"), "blocked_response"),
    ):
        verdict = evaluate_source_feasibility(_source(ranking_expected_raw_row_count=1), _evidence(html))
        assert verdict.reason_code == expected

    redirected = evaluate_source_feasibility(
        _source(ranking_expected_raw_row_count=1),
        _evidence(
            _html(rows),
            resolved_url="https://www.publishnews.com.br/ranking-nielsen/semanal/9/2026-08-17/0/0",
        ),
    )
    assert redirected.reason_code == "unrelated_redirect"


def test_synthetic_test_only_field_selectors_reject_cross_row_metadata() -> None:
    cross_row = (
        '<div class="pn-ranking-livro"><div class="pn-ranking-livro-nome">Livro Adulto</div></div>'
        '<div class="pn-ranking-livro"><div class="pn-ranking-livro-autor">Autora Adulta</div>'
        '<span class="pn-ranking-livro-isbn" hidden>9780306406157</span></div>'
    )
    verdict = evaluate_source_feasibility(_source(ranking_expected_raw_row_count=2), _evidence(_html(cross_row)))
    assert verdict.reason_code == "ranking_row_shape"


def test_selector_identifier_accepts_isbn10_and_rejects_bad_checksum() -> None:
    source = _source(ranking_expected_raw_row_count=1)
    valid = evaluate_source_feasibility(
        source,
        _evidence(_html(_row(1, "Livro Adulto", "Autora Adulta", "0306406152"))),
    )
    assert valid.accepted
    assert valid.samples[0].identifier == "0306406152"

    invalid = evaluate_source_feasibility(
        source,
        _evidence(_html(_row(1, "Livro Adulto", "Autora Adulta", "0306406153"))),
    )
    assert invalid.reason_code == "ranking_row_shape"
    assert "identifier" in invalid.reason


def test_synthetic_test_only_identifier_pattern_requires_one_valid_isbn_per_row() -> None:
    pattern_source = _source(
        ranking_expected_raw_row_count=2,
        ranking_identifier_selector="",
        ranking_identifier_pattern=r"(?P<identifier>97[89]\d{10})",
    )
    valid = _evidence(
        _html(
            '<div class="pn-ranking-livro"><div class="pn-ranking-livro-nome">Livro Adulto</div>'
            '<div class="pn-ranking-livro-autor">Autora Adulta</div>'
            "<span hidden>ISBN 9780306406157</span></div>"
            '<div class="pn-ranking-livro"><div class="pn-ranking-livro-nome">Outro Livro</div>'
            '<div class="pn-ranking-livro-autor">Outra Autora</div>'
            "<span hidden>ISBN 9780140328721</span></div>"
        )
    )
    verdict = evaluate_source_feasibility(pattern_source, valid)
    assert verdict.accepted
    assert [sample.identifier for sample in verdict.samples] == ["9780306406157", "9780140328721"]

    multiple = valid["html"].replace("ISBN 9780306406157", "ISBN 9780306406157 ISBN 9780140328721", 1)
    verdict = evaluate_source_feasibility(pattern_source, _evidence(multiple))
    assert verdict.reason_code == "ranking_row_shape"
    assert "identifier" in verdict.reason and "matched 2" in verdict.reason

    hidden_prose = valid["html"].replace("ISBN 9780306406157", "ISBN is not available", 1)
    verdict = evaluate_source_feasibility(pattern_source, _evidence(hidden_prose))
    assert verdict.reason_code == "ranking_row_shape"
    assert "identifier" in verdict.reason


def test_synthetic_test_only_reporting_period_is_ordered_and_fresh() -> None:
    source = _source(expected_reporting_period="", ranking_expected_raw_row_count=1)
    evidence = _evidence(_html(_row(1, "Livro Adulto", "Autora Adulta", "9780306406157")))
    evidence["reference_date"] = "2026-08-10"
    assert evaluate_source_feasibility(source, evidence).accepted
    for period in (
        "03/08/2025 a 09/08/2025",
        "10/08/2026 a 17/08/2026",
        "10/08/2026 a 09/08/2026",
    ):
        heading = HEADING.replace("03/08/2026 a 09/08/2026", period)
        verdict = evaluate_source_feasibility(
            source,
            {**evidence, "html": _html(_row(1, "Livro Adulto", "Autora Adulta", "9780306406157"), heading=heading)},
        )
        assert verdict.reason_code == "ranking_page_identity"


def test_synthetic_test_only_duplicate_rows_do_not_create_extra_samples() -> None:
    rows = _row(1, "Livro Adulto", "Autora Adulta", "9780306406157") + _row(
        2, "Livro Adulto", "Autora Adulta", "9780306406157"
    )
    verdict = evaluate_source_feasibility(_source(ranking_expected_raw_row_count=2), _evidence(_html(rows)))
    assert verdict.accepted
    assert verdict.bounded_entry_count == 1
    assert all(sample.title != "Advertisement" for sample in verdict.samples)


def _helper(entries: list[tuple[str, str, str]]) -> dict[str, Any]:
    return {
        "version": 1,
        "source_url": URL,
        "language": "pt",
        "entries": [
            {
                "title": title,
                "author": author,
                "evidence_url": URL,
                "english_title": f"English {index}",
                "search_translation": f"Русский {index}",
                "author_transliteration_note": f"Author {index}",
            }
            for index, (title, author, _isbn) in enumerate(entries, start=1)
        ],
    }


@pytest.mark.parametrize("reference_date", [None, "not-an-iso-date"])
def test_ranking_replay_fixture_generation_requires_retained_reference_date(
    reference_date: str | None,
) -> None:
    evidence = _evidence(
        _html(_row(1, "Livro Adulto", "Autora Adulta", "9780306406157")),
        contract_family="ranking-table",
        contract_metadata=RANKING_METADATA,
    )
    if reference_date is None:
        evidence.pop("reference_date")
    else:
        evidence["reference_date"] = reference_date
    with pytest.raises(ReplayFixtureError, match="reference"):
        build_replay_fixture_pair(
            URL,
            "publishnews-reference-date",
            evidence,
            source_name="PublishNews synthetic ranking",
            signal_family="bestseller/ranking",
            contract_family="ranking-table",
            contract_metadata=RANKING_METADATA,
            signal_strength="top ranking",
            candidate_category="adult nonfiction",
            language="pt",
            helper_metadata=_helper([("Livro Adulto", "Autora Adulta", URL)]),
        )


def _ranking_replay_extraction() -> dict[str, Any]:
    return {
        "mentions": [
            {
                "title": "Livro Adulto",
                "primary_authors": ["Autora Adulta"],
                "isbn": "9780306406157",
                "current_rank": "1",
                "source_week": "03/08/2026 a 09/08/2026",
                "source_url": URL,
                "evidence_url": URL,
                "signal_type": "bestseller/ranking",
                "signal_strength": "top ranking",
                "candidate_category": "adult nonfiction",
                "evidence_note": "Bounded ranking row on the captured source page.",
                "english_title": "Adult Book",
                "search_translation": "Книга для взрослых",
                "author_transliteration_note": "Автор Адалта",
            }
        ]
    }


def test_source_collection_passes_run_date_to_dynamic_ranking_identity(tmp_path: Path) -> None:
    next_heading = HEADING.replace("03/08/2026 a 09/08/2026", "10/08/2026 a 16/08/2026")
    run_date = "2026-08-17"
    evidence = _evidence(
        _html(_row(1, "Livro Adulto", "Autora Adulta", "9780306406157"), heading=next_heading),
        reference_date=run_date,
        contract_family="ranking-table",
        contract_metadata={**RANKING_METADATA, "ranking_expected_raw_row_count": 1},
        source_name="PublishNews synthetic ranking",
        signal_family="bestseller/ranking",
    )
    verdict = evaluate_source_feasibility(_source(ranking_expected_raw_row_count=1), evidence)
    assert verdict.accepted
    evidence["samples"] = [
        {
            "title": sample.title,
            "author": sample.author,
            "url": sample.url,
            "rank": sample.rank,
            "identifier": sample.identifier,
            "category": sample.category,
            "reporting_period": sample.reporting_period,
        }
        for sample in verdict.samples
    ]
    pair = build_replay_fixture_pair(
        URL,
        "publishnews-next-week",
        evidence,
        source_name="PublishNews synthetic ranking",
        signal_family="bestseller/ranking",
        contract_family="ranking-table",
        contract_metadata={**RANKING_METADATA, "ranking_expected_raw_row_count": 1},
        signal_strength="top ranking",
        candidate_category="adult nonfiction",
        language="pt",
        helper_metadata=_helper([(verdict.samples[0].title, verdict.samples[0].author, verdict.samples[0].identifier)]),
    )
    page = tmp_path / "page.json"
    extraction = tmp_path / "extraction.json"
    page.write_bytes(pair.page_bytes)
    extraction.write_bytes(pair.extraction_bytes)
    source = _source(
        source_page_fixture_file=str(page),
        extraction_response_file=str(extraction),
        extraction_method="codex-extraction-replay",
        ranking_expected_raw_row_count=1,
    )
    result = SourceCollection(tmp_path / "config.json", tmp_path, run_date).collect([source])
    assert result.source_runs[0]["status"] == "collected"
    assert result.source_mentions[0]["source_week"] == "10/08/2026 a 16/08/2026"


def test_live_ranking_page_uses_active_run_date_not_retained_reference_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = {
        "source_page": URL,
        "resolved_url": URL,
        "reference_date": "2026-08-10",
        "html": _html(_row(1, "Livro Adulto", "Autora Adulta", "9780306406157")),
    }
    seen: dict[str, Any] = {}

    def fake_fetch(
        *_args: Any, reference_date: date | None = None, **_kwargs: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        seen["reference_date"] = reference_date
        return page, {"live_fetch": True}

    monkeypatch.setattr("autoscout.source_collectors.fetch_agent_source_page", fake_fetch)
    monkeypatch.setattr(
        "autoscout.source_collectors.live_agent_extraction_response",
        lambda *_args, **_kwargs: (_ranking_replay_extraction(), {"backend_mode": "test"}),
    )
    source = _source(enable_live=True, extraction_method="agent-extraction-live")
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-08-24").collect([source])

    run = result.source_runs[0]
    assert seen["reference_date"] == date(2026, 8, 24)
    assert run["status"] == "failed"
    assert run["debug_metadata"]["safe_failure_status"] == "page_shape"
    assert "reporting period is not current" in run["reason"]


@pytest.mark.parametrize("reference_date", [None, "not-an-iso-date"])
def test_ranking_replay_requires_a_valid_retained_reference_date(tmp_path: Path, reference_date: str | None) -> None:
    page = {
        "source_page": URL,
        "resolved_url": URL,
        "html": _html(_row(1, "Livro Adulto", "Autora Adulta", "9780306406157")),
    }
    if reference_date is not None:
        page["reference_date"] = reference_date
    page_path = tmp_path / "page.json"
    extraction_path = tmp_path / "extraction.json"
    page_path.write_text(json.dumps(page, ensure_ascii=False) + "\n", encoding="utf-8")
    extraction_path.write_text(json.dumps(_ranking_replay_extraction(), ensure_ascii=False) + "\n", encoding="utf-8")
    source = _source(
        source_page_fixture_file=str(page_path),
        extraction_response_file=str(extraction_path),
        extraction_method="codex-extraction-replay",
    )

    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-06-10").collect([source])
    run = result.source_runs[0]
    assert run["status"] == "failed"
    assert run["debug_metadata"]["safe_failure_status"] in {
        "missing_replay_reference_date",
        "invalid_replay_reference_date",
    }


def test_legacy_ranking_replay_without_freshness_declaration_fails_closed_on_row_count(tmp_path: Path) -> None:
    page_path = tmp_path / "legacy-page.json"
    extraction_path = tmp_path / "legacy-extraction.json"
    page_path.write_text(
        json.dumps(
            {
                "source_page": URL,
                "resolved_url": URL,
                "html": _html(_row(1, "Livro Adulto", "Autora Adulta", "9780306406157")),
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    extraction_path.write_text(json.dumps(_ranking_replay_extraction(), ensure_ascii=False) + "\n", encoding="utf-8")
    source = _source(
        source_page_fixture_file=str(page_path),
        extraction_response_file=str(extraction_path),
        extraction_method="codex-extraction-replay",
        ranking_reporting_period_pattern="",
        ranking_reporting_period_date_format="",
        ranking_source_statement="",
    )

    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-06-10").collect([source])
    assert result.source_runs[0]["status"] == "failed"
    assert "expected exactly 20" in result.source_runs[0]["reason"]
    assert result.source_runs[0]["debug_metadata"]["safe_failure_status"] == "page_shape"
    assert result.source_mentions == []


def test_replay_keeps_first_three_qualifying_rows_and_source_order(tmp_path: Path) -> None:
    values = [
        ("Eu duvido você terminar este livro", "Giuliana Mafra", "9780306406157"),
        ("Primeiro livro", "Autora Um", "9780140328721"),
        ("Segundo livro", "Autora Dois", "9781401971366"),
        ("Terceiro livro", "Autora Três", "9780306406157"),
        ("Quarto livro", "Autora Quatro", "9780140328721"),
    ]
    evidence = _evidence(_html("".join(_row(i + 1, *value) for i, value in enumerate(values))))
    evidence["contract_family"] = "ranking-table"
    evidence["contract_metadata"] = {
        **RANKING_METADATA,
        "ranking_expected_raw_row_count": 5,
    }
    evidence["ranking_expected_raw_row_count"] = 5
    evidence["source_name"] = "PublishNews synthetic ranking"
    evidence["signal_family"] = "bestseller/ranking"
    verdict = evaluate_source_feasibility(_source(ranking_expected_raw_row_count=5), evidence)
    assert [sample.rank for sample in verdict.samples] == ["2", "3", "4"]
    evidence["samples"] = [
        {
            "title": sample.title,
            "author": sample.author,
            "url": sample.url,
            "rank": sample.rank,
            "identifier": sample.identifier,
            "category": sample.category,
            "reporting_period": sample.reporting_period,
        }
        for sample in verdict.samples
    ]
    entries = [(sample.title, sample.author, sample.identifier) for sample in verdict.samples]
    pair = build_replay_fixture_pair(
        URL,
        "publishnews-synthetic",
        evidence,
        source_name="PublishNews synthetic ranking",
        signal_family="bestseller/ranking",
        contract_family="ranking-table",
        contract_metadata={**RANKING_METADATA, "ranking_expected_raw_row_count": 5},
        signal_strength="top ranking",
        candidate_category="adult nonfiction",
        language="pt",
        helper_metadata=_helper(entries),
    )
    assert len(ranking_rows(pair.page["html"], {**RANKING_METADATA, "url": URL})) == 5
    assert len(pair.extraction["mentions"]) == 3
    page = tmp_path / "page.json"
    extraction = tmp_path / "extraction.json"
    page.write_bytes(pair.page_bytes)
    extraction.write_bytes(pair.extraction_bytes)
    source = _source(
        source_page_fixture_file=str(page),
        extraction_response_file=str(extraction),
        extraction_method="codex-extraction-replay",
        ranking_expected_raw_row_count=5,
    )
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-08-20").collect([source])
    assert result.source_runs[0]["status"] == "collected"
    assert [item["current_rank"] for item in result.source_mentions] == ["2", "3", "4"]
    assert [item["source_url"] for item in result.source_mentions] == [URL] * 3


def test_publishnews_contract_rejects_page_shape_drift_from_twenty_raw_rows() -> None:
    source = _source(ranking_expected_raw_row_count=20)
    for count in (19, 21):
        rows = "".join(
            _row(index, f"Livro Adulto {index}", "Autora Adulta", f"978-{index}") for index in range(1, count + 1)
        )
        verdict = evaluate_source_feasibility(source, _evidence(_html(rows)))
        assert verdict.reason_code == "ranking_row_shape"
        assert verdict.raw_row_count == count


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"ranking_rank_source": "unsupported"}, "ranking_rank_source"),
        ({"ranking_rank_source": "selector", "ranking_rank_selector": ""}, "ranking_rank_selector"),
        ({"ranking_expected_path": "/wrong"}, "ranking_expected_path"),
        ({"ranking_evidence_url": "resolved_url"}, "ranking_evidence_url"),
        ({"ranking_category_marker": ""}, "ranking_category_marker"),
        ({"ranking_expected_raw_row_count": 0}, "must be positive"),
        ({"ranking_min_raw_row_count": 3}, "cannot be combined"),
        ({"adult_nonfiction_exclusions": []}, "adult_nonfiction_exclusions"),
        ({"ranking_reporting_period_pattern": "("}, "pattern is malformed"),
    ],
)
def test_ranking_config_rejects_page_shape_and_contract_drift(override: dict[str, Any], fragment: str) -> None:
    errors = validate_config({"sources": [_source(**override)]})
    assert any(fragment in error for error in errors)


def test_missing_rank_source_defaults_to_selector_and_fails_without_selector() -> None:
    source = _source()
    source.pop("ranking_rank_source")
    errors = validate_config({"sources": [source]})
    assert any("ranking_rank_selector" in error for error in errors)


def test_ranking_contract_rejects_contradictory_exact_list_flag() -> None:
    errors = validate_config(
        {
            "sources": [
                _source(metadata_exact_book_list=True),
            ]
        }
    )
    assert any("must not set metadata_exact_book_list" in error for error in errors)


def _ranking_preview_case(tmp_path: Path) -> tuple[list[str], dict[str, Path]]:
    repo = tmp_path / "repo"
    (repo / "configs").mkdir(parents=True)
    (repo / "data" / "current-weekly-pilot").mkdir(parents=True)
    (repo / "runs" / "current-weekly-pilot-russian").mkdir(parents=True)
    (repo / "docs").mkdir()
    config_paths = {
        key: repo / "configs" / filename
        for key, filename in (
            ("portfolio", "current-weekly-portfolio.json"),
            ("pilot", "current-weekly-pilot.json"),
            ("pilot_live", "current-weekly-pilot-live.json"),
            ("live", "current-weekly-live.json"),
        )
    }
    for path in config_paths.values():
        path.write_text(json.dumps({"sources": []}) + "\n", encoding="utf-8")
    html = _html(
        _row(
            1,
            "Eu duvido você terminar este livro",
            "Giuliana Mafra",
            "978-0306406157",
            "Outro Planeta",
        )
        + _row(2, "Livro Adulto Um", "Autora Um", "978-0140328721")
        + _row(3, "Livro Adulto Dois", "Autora Dois", "978-1401971366")
        + _row(4, "Livro Adulto Três", "Autora Três", "978-0306406157")
        + "".join(_row(index, f"Extra Livro {index}", "Autora Extra", "978-0306406157") for index in range(5, 21))
    )
    evidence = _evidence(html, contract_family="ranking-table", contract_metadata=RANKING_METADATA)
    evidence_path = repo / "data" / "ranking-evidence.json"
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False) + "\n", encoding="utf-8")
    helper_path = repo / "ranking-helper.json"
    helper_path.write_text(
        json.dumps(
            {
                "version": 1,
                "source_url": URL,
                "language": "pt",
                "entries": [
                    {
                        "title": "Livro Adulto Um",
                        "author": "Autora Um",
                        "evidence_url": URL,
                        "english_title": "Adult Book One",
                        "search_translation": "Книга один",
                        "author_transliteration_note": "Автор один",
                    },
                    {
                        "title": "Livro Adulto Dois",
                        "author": "Autora Dois",
                        "evidence_url": URL,
                        "english_title": "Adult Book Two",
                        "search_translation": "Книга два",
                        "author_transliteration_note": "Автор два",
                    },
                    {
                        "title": "Livro Adulto Três",
                        "author": "Autora Três",
                        "evidence_url": URL,
                        "english_title": "Adult Book Three",
                        "search_translation": "Книга три",
                        "author_transliteration_note": "Автор три",
                    },
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    feasibility = repo / "ranking-feasibility-input.md"
    feasibility.write_text(
        f"# Issue #562 feasibility evidence\n\nSupporting feasibility and Source Proposal work for #560.\n\nSource: PublishNews synthetic ranking\n\nURL: {URL}\n\n"
        "The accepted bounded evidence exposes adult nonfiction ranking metadata.\n",
        encoding="utf-8",
    )
    proposal = repo / "runs" / "current-weekly-pilot-russian" / "source-proposals.json"
    args = [
        "add-source",
        "--url",
        URL,
        "--probe-evidence",
        str(evidence_path),
        "--helper-metadata",
        str(helper_path),
        "--feasibility-evidence",
        str(feasibility),
        "--feasibility-doc",
        str(repo / "docs" / "issue-562-publishnews-source-feasibility.md"),
        "--onboarding-issue",
        "562",
        "--reason",
        "The bounded ranking is a useful adult nonfiction signal.",
        "--source-name",
        "PublishNews synthetic ranking",
        "--family",
        "bestseller/ranking",
        "--signal-family",
        "bestseller/ranking",
        "--access-status",
        "open",
        "--check-frequency",
        "weekly",
        "--language",
        "pt",
        "--country",
        "Brazil",
        "--market",
        "Brazil",
        "--expected-category",
        "Não Ficção Trade",
        "--refresh-cadence",
        "weekly",
        "--url-stability",
        "stable category page",
        "--extraction-notes",
        "Metadata-only ranking rows with authoritative titles and authors.",
        "--expected-page-heading",
        HEADING,
        "--expected-signal-strength",
        "top ranking",
        "--eligible-candidate-category",
        "adult nonfiction",
        "--lifecycle-note",
        "Issue #562 active and reversible during probation.",
        "--notes",
        "Issue #562 accepted ranking source; replay-first onboarding. Supporting feasibility and Source Proposal work is linked to #560.",
        "--proposals-file",
        str(proposal),
        "--portfolio-config",
        str(config_paths["portfolio"]),
        "--pilot-config",
        str(config_paths["pilot"]),
        "--pilot-live-config",
        str(config_paths["pilot_live"]),
        "--live-config",
        str(config_paths["live"]),
        "--repo-root",
        str(repo),
        "--run-date",
        "2026-08-20",
        "--contract-family",
        "ranking-table",
        "--ranking-row-selector",
        RANKING_METADATA["ranking_row_selector"],
        "--ranking-rank-source",
        RANKING_METADATA["ranking_rank_source"],
        "--ranking-title-selector",
        RANKING_METADATA["ranking_title_selector"],
        "--ranking-author-selector",
        RANKING_METADATA["ranking_author_selector"],
        "--ranking-identifier-selector",
        RANKING_METADATA["ranking_identifier_selector"],
        "--ranking-category-marker",
        RANKING_METADATA["ranking_category_marker"],
        "--ranking-reporting-period-pattern",
        RANKING_METADATA["ranking_reporting_period_pattern"],
        "--ranking-reporting-period-date-format",
        RANKING_METADATA["ranking_reporting_period_date_format"],
        "--ranking-reporting-period-max-age-days",
        str(RANKING_METADATA["ranking_reporting_period_max_age_days"]),
        "--ranking-expected-raw-row-count",
        str(RANKING_METADATA["ranking_expected_raw_row_count"]),
        "--ranking-evidence-url",
        "source_page",
        "--ranking-exclusion-term",
        "eu duvido você terminar este livro",
        "--ranking-exclusion-term",
        "colorindo com aquarela",
        "--ranking-exclusion-term",
        "livro de atividades",
    ]
    return args, {"repo": repo, "helper": helper_path, "evidence": evidence_path, **config_paths}


def test_issue_560_feasibility_source_proposal_support_is_gated_for_issue_562(tmp_path: Path, capsys: Any) -> None:
    args, paths = _ranking_preview_case(tmp_path)
    before = {key: path.read_bytes() for key, path in paths.items() if key != "repo" and path.exists()}
    assert main(args) == EXIT_CONFIRMATION
    report = json.loads(capsys.readouterr().out)
    assert report["confirmation_required"] is True
    assert report["representative_entries"]
    assert all(entry["title"] != "Eu duvido você terminar este livro" for entry in report["representative_entries"])
    assert [entry["title"] for entry in report["representative_entries"]] == [
        "Livro Adulto Um",
        "Livro Adulto Dois",
        "Livro Adulto Três",
    ]
    onboarding_metadata = report["onboarding_request"]["metadata"]
    assert onboarding_metadata["ranking_evidence_url"] == "source_page"
    assert onboarding_metadata["ranking_rank_source"] == "document_order"
    assert onboarding_metadata["visible_title_selector"] == RANKING_METADATA["visible_title_selector"]
    assert onboarding_metadata["adult_nonfiction_exclusions"] == [
        "eu duvido você terminar este livro",
        "colorindo com aquarela",
        "livro de atividades",
    ]
    assert "ranking_exclusion_terms" not in onboarding_metadata
    assert onboarding_metadata.get("metadata_exact_book_list") is not True
    assert report["confirmation_token"]
    assert report["artifact_identity"]["probe_evidence"]["sha256"]
    assert report["artifact_identity"]["proposal_state"]["present"] is False
    assert {key: path.read_bytes() for key, path in paths.items() if key != "repo" and path.exists()} == before

    stale = paths["repo"] / "stale-helper.json"
    stale.write_bytes(paths["helper"].read_bytes().replace(b"Livro Adulto Dois", b"Stale Book"))
    stale_args = list(args)
    stale_args[stale_args.index(str(paths["helper"]))] = str(stale)
    assert main(stale_args) != EXIT_CONFIRMATION
    stale_report = json.loads(capsys.readouterr().out)
    assert stale_report["reason_code"] == "helper_metadata_identity_mismatch"
    assert not (paths["repo"] / "docs" / "issue-562-publishnews-source-feasibility.md").exists()


def test_live_source_failure_isolated_from_sibling_replay(tmp_path: Path) -> None:
    good = {
        "name": "Good sibling",
        "family": "award",
        "url": "https://example.test/good",
        "fixture_candidates": [
            {
                "title": "Good book",
                "primary_authors": ["Good author"],
                "source_url": "https://example.test/good",
                "signal_strength": "winner",
                "evidence_note": "A deterministic fixture mention.",
            }
        ],
    }
    page = tmp_path / "live-page.json"
    page.write_text(json.dumps({"source_page": URL, "resolved_url": URL, "html": "<h1>Failure page</h1>"}))
    bad = {
        "name": "PublishNews live failure",
        "family": "bestseller/ranking",
        "url": URL,
        "extraction_method": "agent-extraction-live",
        "enable_live": True,
        "extraction_backend": "missing-backend",
        "source_page_fixture_file": str(page),
    }
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-08-20").collect([bad, good])
    assert result.source_runs[0]["status"] == "failed"
    assert result.source_runs[1]["status"] == "collected"
    assert [mention["title"] for mention in result.source_mentions] == ["Good book"]
