"""Offline vertical-slice regression tests for PublishNews round 13.

The weekly run dropped every PublishNews row because the extraction agent
duplicated the Portuguese title into ``english_title`` and
``search_translation``. A duplicated helper value is not a translation, so it
must not block the curated sidecar, and the extraction prompt must state the
rule the contract enforces.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from autoscout.codex_recovery import build_codex_extraction_prompt
from autoscout.source_collection import SourceCollection
from autoscout.source_collectors import agent_extraction_prompt

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "issue-562-publishnews-ranking"
URL = "https://www.publishnews.com.br/ranking-nielsen/semanal/9"
HEADING = "Lista Nielsen-PublishNews de Mais Vendidos de Não Ficção Trade de 03/08/2026 a 09/08/2026"

DUPLICATED_HELPER_TITLES = ("Bom dia, inverno", "A psicologia financeira", "Pai, me conta a sua história?")


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


def _extraction_with_duplicated_helpers(tmp_path: Path) -> Path:
    """Reproduce the weekly artifact that copied the title into every helper."""
    extraction = json.loads((FIXTURE_DIR / "extraction.json").read_text(encoding="utf-8"))
    for entry in extraction["entries"]:
        entry["english_title"] = entry["title"]
        entry["search_translation"] = entry["title"]
        entry["author_transliteration_note"] = (
            "Preserve the source spelling; no authoritative transliteration is provided in the capture."
        )
    path = tmp_path / "duplicated-helper-extraction.json"
    path.write_text(json.dumps(extraction, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def test_ranking_prompt_states_the_translation_rule_the_contract_enforces() -> None:
    prompt = agent_extraction_prompt(_source(), {"html": "<h1>Ranking</h1>"}, 4000)
    assert "search_translation must be nonblank and different from the authoritative title" in prompt
    assert "Never duplicate the authoritative title into search_translation" in prompt


def test_publishnews_replay_output_matches_committed_baseline_fields(tmp_path: Path) -> None:
    source = _source(helper_metadata_file=FIXTURE_DIR / "helper-metadata.json")
    result = SourceCollection(tmp_path / "config.json", tmp_path / "run", "2026-08-22").collect([source])
    baseline = json.loads((FIXTURE_DIR / "extraction.json").read_text(encoding="utf-8"))["entries"][:3]
    fields = tuple(sorted({field for entry in baseline for field in entry}))
    actual = [{field: mention.get(field) for field in fields} for mention in result.source_mentions]
    expected = [
        {
            field: (str(entry.get(field)) if field == "current_rank" and field in entry else entry.get(field))
            for field in fields
        }
        for entry in baseline
    ]
    assert actual == expected


def test_duplicated_model_helpers_do_not_block_the_curated_sidecar(tmp_path: Path) -> None:
    source = _source(helper_metadata_file=FIXTURE_DIR / "helper-metadata.json")
    source["extraction_response_file"] = str(_extraction_with_duplicated_helpers(tmp_path))

    result = SourceCollection(tmp_path / "config.json", tmp_path / "run", "2026-08-22").collect([source])
    run = result.source_runs[0]

    assert run["status"] == "collected"
    assert run["valid_mention_count"] == 3
    assert [mention["title"] for mention in result.source_mentions] == list(DUPLICATED_HELPER_TITLES)
    assert [mention["english_title"] for mention in result.source_mentions] == [
        "Good Morning, Winter",
        "The Psychology of Money",
        "Dad, Tell Me Your Story?",
    ]
    assert [mention["search_translation"] for mention in result.source_mentions] == [
        "Доброе утро, зима",
        "Психология денег",
        "Папа, расскажи мне свою историю",
    ]


def test_duplicated_model_helpers_without_a_sidecar_row_fail_with_a_truthful_reason(tmp_path: Path) -> None:
    source = _source()
    source["extraction_response_file"] = str(_extraction_with_duplicated_helpers(tmp_path))

    result = SourceCollection(tmp_path / "config.json", tmp_path / "run", "2026-08-22").collect([source])
    run = result.source_runs[0]

    assert run["status"] == "failed"
    assert run["valid_mention_count"] == 0
    assert all("helper metadata is missing" in drop["reason"] for drop in run["drop_reasons"])


def test_codex_worklist_prompt_states_the_translation_rule_the_contract_enforces() -> None:
    """The weekly run extracts through the Codex worklist prompt, not the live one."""
    prompt = build_codex_extraction_prompt(
        {
            "source_name": "PublishNews Nielsen Não Ficção Trade",
            "source_url": URL,
            "source_family": "bestseller/ranking",
            "operation_id": "round-13",
            "capture_sha256": "0" * 64,
            "source_metadata": _source(),
        }
    )
    assert "search_translation must be nonblank and different from the authoritative title" in prompt


def test_codex_replay_config_keeps_the_ranking_table_declaration(tmp_path: Path) -> None:
    """The generated replay config must validate without a ranking-table warning."""
    from autoscout.codex_workflow import finalize_codex_run
    from autoscout.codex_workflow import prepare_codex_extraction
    from autoscout.config import validate_config

    source = _source()
    config_path = tmp_path / "config.json"
    output_dir = tmp_path / "run"
    config_path.write_text(json.dumps({"run_date": "2026-08-22", "sources": [source]}) + "\n", encoding="utf-8")

    assert prepare_codex_extraction(config_path, output_dir, effective_date="2026-08-22") == 0
    item = json.loads((output_dir / "codex-extraction-worklist.json").read_text(encoding="utf-8"))["work_items"][0]
    assert item["source_metadata"]["metadata_ranking_table"] is True

    extraction_path = output_dir / item["expected_extraction_file"]
    extraction_path.write_bytes((FIXTURE_DIR / "extraction.json").read_bytes())
    assert finalize_codex_run(output_dir) == 0

    replay_config = json.loads((output_dir / "codex-replay-config.json").read_text(encoding="utf-8"))
    assert replay_config["sources"][0]["metadata_ranking_table"] is True
    assert validate_config(replay_config) == []


def test_an_english_sidecar_never_overwrites_a_matching_english_title(tmp_path: Path) -> None:
    """An English list requires english_title to equal the title, so a duplicate there is correct."""
    from autoscout.helper_metadata import HelperMetadataIndex
    from autoscout.helper_metadata import enrich_with_helper_metadata

    identity = ("Some English Title", "An Author", "https://example.test/list")
    sidecar = {
        "english_title": "A Different English Title",
        "search_translation": "Русский перевод",
        "author_transliteration_note": "Автор",
    }
    entry = {
        "title": identity[0],
        "author": identity[1],
        "evidence_url": identity[2],
        "english_title": identity[0],
        "search_translation": identity[0],
    }

    for language, expected_english in (("en", identity[0]), ("pt", "A Different English Title")):
        index = HelperMetadataIndex(
            source_url=identity[2], language=language, entries={identity: sidecar}, sha256="", raw_bytes=b""
        )
        enriched, matched = enrich_with_helper_metadata(entry, index)
        assert matched == identity
        assert enriched["english_title"] == expected_english
        # A duplicated search_translation always loses to the sidecar.
        assert enriched["search_translation"] == "Русский перевод"
