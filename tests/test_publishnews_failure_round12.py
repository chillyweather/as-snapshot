"""Offline vertical-slice regression tests for the PublishNews weekly failure."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from autoscout.codex_workflow import finalize_codex_run
from autoscout.codex_workflow import prepare_codex_extraction
from autoscout.config import load_validated_config
from autoscout.pipeline import run_evidence_digest
from autoscout.source_collection import SourceCollection
from autoscout.source_collectors import agent_extraction_prompt
from autoscout.source_page_identity import parse_visible_page_identity
from autoscout.source_page_identity import ranking_mention_is_grounded
from autoscout.source_page_identity import ranking_rows

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "issue-562-publishnews-ranking"
URL = "https://www.publishnews.com.br/ranking-nielsen/semanal/9"
HEADING = "Lista Nielsen-PublishNews de Mais Vendidos de Não Ficção Trade de 03/08/2026 a 09/08/2026"


def _source(*, helper_metadata_file: Path | None = None) -> dict[str, Any]:
    source: dict[str, Any] = {
        "name": "PublishNews Nielsen Não Ficção Trade",
        "family": "bestseller/ranking",
        "url": URL,
        "active": True,
        "access_status": "open",
        "check_frequency": "weekly",
        "language": "pt",
        "expected_page_heading": HEADING,
        "expected_signal_strength": "top ranking",
        "eligible_candidate_category": "adult nonfiction",
        "signal_family": "bestseller/ranking",
        "contract_family": "ranking-table",
        "metadata_ranking_table": True,
        "ranking_evidence_url": "source_page",
        "visible_title_selector": ".pn-colunas-06x07 > .pn-colunas-corpo > .pn-editoria > .pn-editoria-cabeca > .pn-editoria-titulo",
        "ranking_row_selector": ".pn-ranking-livro",
        "ranking_rank_source": "document_order",
        "ranking_title_selector": ".pn-ranking-livro-nome",
        "ranking_author_selector": ".pn-ranking-livro-autor",
        "ranking_identifier_pattern": r"(?P<identifier>97[89][0-9-]{10,20})",
        "ranking_category_marker": "Não Ficção Trade",
        "ranking_reporting_period_pattern": r"(?P<period>[0-9]{2}/[0-9]{2}/[0-9]{4} a [0-9]{2}/[0-9]{2}/[0-9]{4})",
        "ranking_reporting_period_date_format": "%d/%m/%Y",
        "ranking_reporting_period_max_age_days": 14,
        "ranking_expected_raw_row_count": 20,
        "adult_nonfiction_exclusions": [
            "eu duvido você terminar este livro",
            "colorindo com aquarela",
            "livro de atividades",
        ],
        "source_page_fixture_file": str(FIXTURE_DIR / "page.json"),
        "extraction_response_file": str(FIXTURE_DIR / "extraction.json"),
        "extraction_method": "codex-extraction-replay",
    }
    if helper_metadata_file is not None:
        source["helper_metadata_file"] = str(helper_metadata_file)
    return source


def test_publishnews_helper_path_stays_out_of_extraction_prompt(tmp_path: Path) -> None:
    helper = tmp_path / "helper.json"
    helper.write_bytes((FIXTURE_DIR / "helper-metadata.json").read_bytes())
    source = _source(helper_metadata_file=helper)
    prompt = agent_extraction_prompt(source, {"html": "<h1>Ranking</h1>"}, 1000)
    assert str(helper) not in prompt
    assert "helper_metadata_file" not in prompt


def test_minimized_publishnews_fixture_preserves_only_load_bearing_rows() -> None:
    page = json.loads((FIXTURE_DIR / "page.json").read_text(encoding="utf-8"))
    extraction = json.loads((FIXTURE_DIR / "extraction.json").read_text(encoding="utf-8"))
    rows = ranking_rows(page["html"], _source())
    heading = parse_visible_page_identity(page["html"], _source()["visible_title_selector"])
    assert page["reference_date"] == "2026-08-22"
    assert heading.identity_texts == (HEADING,)
    assert len(rows) == 20
    assert [(rows[index].title, rows[index].author) for index in (0, 1, 2, 9, 15)] == [
        ("Eu duvido você terminar este livro", "Giuliana Mafra"),
        ("Bom dia, inverno", "Tamara Klink"),
        ("A psicologia financeira", "Morgan Housel"),
        ("Deixa pra lá", "Mel Robbins / Sawyer Robbins"),
        ("Diário estoico", "Ryan Holiday / Stephen Hanselman"),
    ]
    assert len(extraction["entries"]) == 19
    assert [entry["title"] for entry in extraction["entries"][:3]] == [
        "Bom dia, inverno",
        "A psicologia financeira",
        "Pai, me conta a sua história?",
    ]


def test_publishnews_evidence_digest_changes_when_helper_sidecar_changes(tmp_path: Path) -> None:
    helper = tmp_path / "helper.json"
    helper.write_bytes((FIXTURE_DIR / "helper-metadata.json").read_bytes())
    source = _source(helper_metadata_file=helper)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"sources": [source]}, ensure_ascii=False) + "\n", encoding="utf-8")
    first = run_evidence_digest(json.loads(config_path.read_text(encoding="utf-8")), config_path, "2026-08-22")
    helper.write_text(
        helper.read_text(encoding="utf-8").replace("Good Morning, Winter", "Changed helper"), encoding="utf-8"
    )
    second = run_evidence_digest(json.loads(config_path.read_text(encoding="utf-8")), config_path, "2026-08-22")
    assert first != second


def test_publishnews_configured_helper_digest_fails_closed_on_byte_change(tmp_path: Path) -> None:
    helper = tmp_path / "helper.json"
    helper.write_bytes((FIXTURE_DIR / "helper-metadata.json").read_bytes())
    source = _source(helper_metadata_file=helper)
    source["helper_metadata_sha256"] = hashlib.sha256(helper.read_bytes()).hexdigest()
    helper.write_bytes(helper.read_bytes().replace(b"Good Morning, Winter", b"Changed helper value"))

    result = SourceCollection(tmp_path / "config.json", tmp_path / "run", "2026-08-22").collect([source])

    run = result.source_runs[0]
    assert run["status"] == "failed"
    assert run["debug_metadata"]["safe_failure_status"] == "helper_metadata_stale"


def test_publishnews_portfolio_resolution_carries_the_confirmed_helper_sidecar() -> None:
    for config_name in (
        "current-weekly-portfolio.json",
        "current-weekly-pilot.json",
        "current-weekly-pilot-live.json",
        "current-weekly-live.json",
    ):
        config_path = ROOT / "configs" / config_name
        config = load_validated_config(config_path)
        source = next(item for item in config["sources"] if item.get("name") == "PublishNews Nielsen Não Ficção Trade")
        helper_path = Path(source["helper_metadata_file"])
        if not helper_path.is_absolute():
            helper_path = config_path.parent / helper_path
        assert (
            helper_path.resolve()
            == (ROOT / "data/current-weekly-pilot/publishnews-nielsen-nao-ficcao-trade-helper-metadata.json").resolve()
        )
        assert helper_path.is_file()
        assert source["helper_metadata_sha256"] == "339b822798c7e8950a7bff91234eecb385604fb95c9033656c980a6f0d9ff73d"


@pytest.mark.parametrize(
    ("mutation", "expected_status"),
    [
        ("missing", "helper_metadata_missing"),
        ("malformed", "helper_metadata_unreadable"),
        ("wrong_url", "helper_metadata_stale"),
        ("duplicate", "helper_metadata_duplicate_entry"),
    ],
)
def test_publishnews_helper_sidecar_fail_closed(tmp_path: Path, mutation: str, expected_status: str) -> None:
    helper = json.loads((FIXTURE_DIR / "helper-metadata.json").read_text(encoding="utf-8"))
    helper_path = tmp_path / "helper.json"
    if mutation == "missing":
        helper_path = tmp_path / "missing.json"
    elif mutation == "malformed":
        helper_path.write_text("not json\n", encoding="utf-8")
    elif mutation == "wrong_url":
        helper["source_url"] = "https://other.example.test/ranking"
        helper_path.write_text(json.dumps(helper) + "\n", encoding="utf-8")
    elif mutation == "duplicate":
        helper["entries"].append(dict(helper["entries"][0]))
        helper_path.write_text(json.dumps(helper, ensure_ascii=False) + "\n", encoding="utf-8")
    else:
        raise AssertionError(mutation)

    source = _source(helper_metadata_file=helper_path)
    result = SourceCollection(tmp_path / "config.json", tmp_path / "run", "2026-08-22").collect([source])
    run = result.source_runs[0]
    assert run["status"] == "failed"
    assert run["debug_metadata"]["safe_failure_status"] == expected_status


def test_publishnews_helper_rotation_is_safe_and_does_not_apply_unrelated_helpers(tmp_path: Path) -> None:
    helper = json.loads((FIXTURE_DIR / "helper-metadata.json").read_text(encoding="utf-8"))
    helper["entries"].append(
        {
            "title": "Historical ranking entry",
            "author": "Historical Author",
            "evidence_url": URL,
            "english_title": "Historical ranking entry in English",
            "search_translation": "Историческая запись рейтинга",
            "author_transliteration_note": "Исторический автор",
        }
    )
    helper_path = tmp_path / "rotated-helper.json"
    helper_path.write_text(json.dumps(helper, ensure_ascii=False) + "\n", encoding="utf-8")

    result = SourceCollection(tmp_path / "config.json", tmp_path / "run", "2026-08-22").collect(
        [_source(helper_metadata_file=helper_path)]
    )

    run = result.source_runs[0]
    assert run["status"] == "collected"
    assert run["valid_mention_count"] == 3
    assert run["dropped_mention_count"] == 16
    assert {mention["title"] for mention in result.source_mentions} == {
        "Bom dia, inverno",
        "A psicologia financeira",
        "Pai, me conta a sua história?",
    }
    assert all("Historical" not in json.dumps(mention, ensure_ascii=False) for mention in result.source_mentions)


def test_compliant_live_ranking_extraction_keeps_both_primary_authors(tmp_path: Path) -> None:
    page = json.loads((FIXTURE_DIR / "page.json").read_text(encoding="utf-8"))
    page["source_name"] = "Synthetic Portuguese ranking"
    page["source_url"] = "https://example.test/ranking"
    page["source_page"] = "https://example.test/ranking"
    page["resolved_url"] = "https://example.test/ranking"
    page["html"] = (
        page["html"]
        .replace("Deixa pra lá", "Synthetic Portuguese Book")
        .replace("Mel Robbins / Sawyer Robbins", "Ana Example / Bia Example")
    )
    page_path = tmp_path / "page.json"
    page_path.write_text(json.dumps(page, ensure_ascii=False) + "\n", encoding="utf-8")
    extraction_path = tmp_path / "extraction.json"
    extraction_path.write_text(
        json.dumps(
            {
                "mentions": [
                    {
                        "title": "Synthetic Portuguese Book",
                        "primary_authors": ["Ana Example", "Bia Example"],
                        "isbn": "978-65-57124-95-6",
                        "current_rank": "10",
                        "source_week": "03/08/2026 a 09/08/2026",
                        "source_url": "https://example.test/ranking",
                        "evidence_url": "https://example.test/ranking",
                        "signal_type": "bestseller/ranking",
                        "signal_strength": "top ranking",
                        "candidate_category": "adult nonfiction",
                        "evidence_note": "Rank 10 na semana de 03/08/2026 a 09/08/2026.",
                        "english_title": "Synthetic Book",
                        "search_translation": "Синтетическая книга",
                        "author_transliteration_note": "Ана Example и Биа Example",
                    }
                ]
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    source = _source()
    source.update(
        {
            "name": "Synthetic Portuguese ranking",
            "url": "https://example.test/ranking",
            "extraction_method": "agent-extraction-live",
            "enable_live": True,
            "source_page_fixture_file": str(page_path),
            "extraction_response_file": str(extraction_path),
        }
    )

    result = SourceCollection(tmp_path / "config.json", tmp_path / "run", "2026-08-22").collect([source])
    run = result.source_runs[0]
    assert run["status"] == "collected"
    assert len(result.source_mentions) == 1
    assert result.source_mentions[0]["primary_authors"] == ["Ana Example", "Bia Example"]


def test_publishnews_helper_sidecar_symlink_fails_closed(tmp_path: Path) -> None:
    link = tmp_path / "helper-link.json"
    try:
        link.symlink_to(FIXTURE_DIR / "helper-metadata.json")
    except OSError:
        pytest.skip("symlink creation is unavailable")
    result = SourceCollection(tmp_path / "config.json", tmp_path / "run", "2026-08-22").collect(
        [_source(helper_metadata_file=link)]
    )
    assert result.source_runs[0]["status"] == "failed"
    assert result.source_runs[0]["debug_metadata"]["safe_failure_status"] == "helper_metadata_unsafe"


def test_publishnews_helper_sidecar_never_overwrites_nonblank_model_helpers(tmp_path: Path) -> None:
    extraction = json.loads((FIXTURE_DIR / "extraction.json").read_text(encoding="utf-8"))
    extraction["entries"][0].update(
        {
            "english_title": "Model supplied title",
            "search_translation": "Модельный поиск",
            "author_transliteration_note": "Модельная заметка",
        }
    )
    extraction_path = tmp_path / "extraction.json"
    extraction_path.write_text(json.dumps(extraction, ensure_ascii=False) + "\n", encoding="utf-8")
    source = _source(helper_metadata_file=FIXTURE_DIR / "helper-metadata.json")
    source["extraction_response_file"] = str(extraction_path)

    result = SourceCollection(tmp_path / "config.json", tmp_path / "run", "2026-08-22").collect([source])
    first = result.source_mentions[0]
    assert first["english_title"] == "Model supplied title"
    assert first["search_translation"] == "Модельный поиск"
    assert first["author_transliteration_note"] == "Модельная заметка"


def test_publishnews_multi_author_grounding_requires_complete_ordered_author_list() -> None:
    page = json.loads((FIXTURE_DIR / "page.json").read_text(encoding="utf-8"))
    source = _source()
    common = {
        "html_content": page["html"],
        "title": "Deixa pra lá",
        "isbn": "978-65-57124-95-6",
        "current_rank": "10",
        "source_week": "03/08/2026 a 09/08/2026",
        "ranking_source": source,
    }
    for separator in ("/", "&", "and", "e"):
        variant = page["html"].replace("Mel Robbins / Sawyer Robbins", f"Mel Robbins {separator} Sawyer Robbins")
        assert ranking_mention_is_grounded(
            **{**common, "html_content": variant},
            primary_authors=["Mel Robbins", "Sawyer Robbins"],
        )
    assert not ranking_mention_is_grounded(**common, primary_authors=["Mel Robbins"])
    assert not ranking_mention_is_grounded(**common, primary_authors=["Sawyer Robbins", "Mel Robbins"])
    assert not ranking_mention_is_grounded(**common, primary_authors=["Mel Robbins", "Sawyer Robbins", "Extra Author"])
    assert not ranking_mention_is_grounded(**common, primary_authors=["Ryan Holiday", "Stephen Hanselman"])


def test_publishnews_worklist_finalize_replay_carries_portable_helper_artifact(tmp_path: Path) -> None:
    helper = FIXTURE_DIR / "helper-metadata.json"
    source = _source(helper_metadata_file=helper)
    source["helper_metadata_sha256"] = hashlib.sha256(helper.read_bytes()).hexdigest()
    config_path = tmp_path / "config.json"
    output_dir = tmp_path / "run"
    config_path.write_text(json.dumps({"run_date": "2026-08-22", "sources": [source]}) + "\n", encoding="utf-8")

    assert prepare_codex_extraction(config_path, output_dir, effective_date="2026-08-22") == 0
    item = json.loads((output_dir / "codex-extraction-worklist.json").read_text(encoding="utf-8"))["work_items"][0]
    metadata = item["source_metadata"]
    helper_filename = metadata["helper_metadata_file"]
    assert Path(helper_filename).name == helper_filename
    assert not Path(helper_filename).is_absolute()
    assert helper_filename == output_dir.joinpath(helper_filename).name
    assert str(helper) not in json.dumps(item, ensure_ascii=False)
    assert "helper_metadata_file" not in json.dumps(item["extraction_contract"], ensure_ascii=False)
    assert str(helper) not in (output_dir / "source-runs.json").read_text(encoding="utf-8")
    assert (output_dir / helper_filename).read_bytes() == helper.read_bytes()
    assert metadata["helper_metadata_sha256"] == hashlib.sha256(helper.read_bytes()).hexdigest()

    extraction_path = output_dir / item["expected_extraction_file"]
    extraction_path.write_bytes((FIXTURE_DIR / "extraction.json").read_bytes())
    assert finalize_codex_run(output_dir) == 0

    replay_config_path = output_dir / "codex-replay-config.json"
    replay_config = json.loads(replay_config_path.read_text(encoding="utf-8"))
    replay_source = replay_config["sources"][0]
    assert replay_source["helper_metadata_file"] == helper_filename
    assert not Path(replay_source["helper_metadata_file"]).is_absolute()
    assert str(helper) not in replay_config_path.read_text(encoding="utf-8")
    assert (replay_config_path.parent / replay_source["helper_metadata_file"]).read_bytes() == helper.read_bytes()

    result = SourceCollection(replay_config_path, tmp_path / "replay", "2026-08-22").collect(replay_config["sources"])
    run = result.source_runs[0]
    assert run["status"] == "collected"
    assert run["valid_mention_count"] == 3
    assert run["dropped_mention_count"] == 16


def test_publishnews_replay_uses_only_confirmed_helper_sidecar_rows(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"sources": []}) + "\n", encoding="utf-8")

    before = SourceCollection(config_path, tmp_path / "before", "2026-08-22").collect([_source()])
    before_run = before.source_runs[0]
    assert before_run["status"] == "failed"
    assert before_run["valid_mention_count"] == 0
    assert before_run["dropped_mention_count"] == 19
    assert sum("helper metadata is missing" in drop["reason"] for drop in before_run["drop_reasons"]) == 19

    helper = FIXTURE_DIR / "helper-metadata.json"
    after = SourceCollection(config_path, tmp_path / "after", "2026-08-22").collect(
        [_source(helper_metadata_file=helper)]
    )
    after_run = after.source_runs[0]
    assert after_run["status"] == "collected"
    assert after_run["valid_mention_count"] == 3
    assert len(after.source_mentions) == 3
    assert [mention["title"] for mention in after.source_mentions] == [
        "Bom dia, inverno",
        "A psicologia financeira",
        "Pai, me conta a sua história?",
    ]
    assert [mention["primary_authors"] for mention in after.source_mentions] == [
        ["Tamara Klink"],
        ["Morgan Housel"],
        ["Elma Van Vliet"],
    ]
    assert after_run["dropped_mention_count"] == 16
    assert all("helper metadata is missing" in drop["reason"] for drop in after_run["drop_reasons"])
