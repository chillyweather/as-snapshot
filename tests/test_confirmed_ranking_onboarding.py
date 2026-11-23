"""Confirmed offline onboarding regression tests for ranking layout metadata."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from autoscout.__main__ import main
from autoscout.cli.weekly import run_mvp
from autoscout.source_onboarding import OnboardingRunners
from autoscout.source_onboarding_transaction import default_replay_runner
from autoscout.source_page_identity import ranking_rows

RANKING_URL = "https://www.publishnews.com.br/ranking-nielsen/semanal/9"
RANKING_SOURCE_NAME = "PublishNews synthetic ranking"
RANKING_HEADING = "Lista Nielsen-PublishNews de Mais Vendidos de Não Ficção Trade de 03/08/2026 a 09/08/2026"
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
    "entry_scope_selector": ".pn-ranking-list > .pn-ranking-livro",
    "ranking_source_statement": "The source ranks nonfiction books by the declared document order for 03/08/2026 a 09/08/2026.",
    "ranking_expected_path": "/ranking-nielsen/semanal/9",
}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _init_git(repo: Path) -> None:
    for args in (("init", "-q"), ("config", "user.name", "Test"), ("config", "user.email", "test@example.test")):
        result = subprocess.run(["git", *args], cwd=repo, check=False, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    result = subprocess.run(["git", "add", "."], cwd=repo, check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    result = subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _isbn(seed: int) -> str:
    digits = f"97803064{seed:04d}"
    checksum = sum((1 if index % 2 == 0 else 3) * int(value) for index, value in enumerate(digits))
    return digits + str((10 - checksum % 10) % 10)


def _ranking_row(title: str, author: str, isbn: str) -> str:
    return (
        '<div class="pn-ranking-livro">'
        f'<div class="pn-ranking-livro-nome">{title}</div>'
        f'<div class="pn-ranking-livro-autor">{author}</div>'
        f'<span class="pn-ranking-livro-isbn" hidden>{isbn}</span>'
        "</div>"
    )


def _ranking_case(tmp_path: Path) -> dict[str, Any]:
    repo = tmp_path / "repo"
    (repo / "configs").mkdir(parents=True)
    (repo / "data" / "current-weekly-pilot").mkdir(parents=True)
    (repo / "docs").mkdir()
    portfolio = repo / "configs" / "current-weekly-portfolio.json"
    _write_json(portfolio, {"sources": [{"name": "Existing", "url": "https://existing.test", "language": "en"}]})
    configs: list[Path] = []
    for filename in ("current-weekly-pilot.json", "current-weekly-pilot-live.json", "current-weekly-live.json"):
        config = repo / "configs" / filename
        configs.append(config)
        _write_json(
            config,
            {"run_date": "2026-06-10", "weekly_portfolio": portfolio.name, "sources": [{"name": "Existing"}]},
        )

    evidence = repo / "data" / "source-candidate-captures" / "publishnews-retained.json"
    rows = _ranking_row("Eu duvido você terminar este livro", "Giuliana Mafra", "9780306406157")
    qualifying = (
        ("Livro Adulto", "Autora Adulta", "9780140328721"),
        ("Livro Dois", "Autora Dois", "9781401971366"),
        ("Livro Tres", "Autora Tres", "9780306406157"),
    )
    rows += "".join(_ranking_row(*entry) for entry in qualifying)
    rows += "".join(
        _ranking_row(f"Omitted ranking row {rank}", f"Omitted author {rank}", _isbn(rank)) for rank in range(5, 21)
    )
    _write_json(
        evidence,
        {
            "collection_mode": "source-candidate-probe",
            "source_url": RANKING_URL,
            "source_page": RANKING_URL,
            "resolved_url": RANKING_URL,
            "head_status": 200,
            "get_status": 200,
            "reference_date": "2026-08-10",
            "html": (
                "<!doctype html><html><body>"
                '<div class="pn-colunas-06x07"><div class="pn-colunas-corpo"><div class="pn-editoria">'
                f'<div class="pn-editoria-cabeca"><div class="pn-editoria-titulo">{RANKING_HEADING}</div></div>'
                f"</div></div></div>{rows}</body></html>"
            ),
            "contract_family": "ranking-table",
            "contract_metadata": RANKING_METADATA,
            "entry_scope_selector": RANKING_METADATA["entry_scope_selector"],
            "ranking_source_statement": RANKING_METADATA["ranking_source_statement"],
            "visible_title_selector": RANKING_METADATA["visible_title_selector"],
            "ranking_expected_path": RANKING_METADATA["ranking_expected_path"],
            "source_name": RANKING_SOURCE_NAME,
            "signal_family": "bestseller/ranking",
        },
    )
    helper = repo / "helper.json"
    _write_json(
        helper,
        {
            "version": 1,
            "source_url": RANKING_URL,
            "language": "pt",
            "entries": [
                {
                    "title": title,
                    "author": author,
                    "evidence_url": RANKING_URL,
                    "english_title": english_title,
                    "search_translation": translation,
                    "author_transliteration_note": transliteration,
                }
                for title, author, english_title, translation, transliteration in (
                    ("Livro Adulto", "Autora Adulta", "Adult Book", "Книга для взрослых", "Автор Адалта"),
                    ("Livro Dois", "Autora Dois", "Book Two", "Книга два", "Автор Два"),
                    ("Livro Tres", "Autora Tres", "Book Three", "Книга три", "Автор Три"),
                )
            ],
        },
    )
    feasibility = repo / "feasibility-draft.md"
    feasibility.write_text(
        f"# Issue #401 feasibility evidence\n\nSource: {RANKING_SOURCE_NAME}\n\nURL: {RANKING_URL}\n\n"
        "The retained evidence confirms a bounded nonfiction ranking table.\n",
        encoding="utf-8",
    )
    args = [
        "add-source",
        "--url",
        RANKING_URL,
        "--probe-evidence",
        str(evidence),
        "--helper-metadata",
        str(helper),
        "--feasibility-evidence",
        str(feasibility),
        "--feasibility-doc",
        str(repo / "docs" / "issue-401-brazil-source-feasibility.md"),
        "--onboarding-issue",
        "401",
        "--run-date",
        "2026-06-10",
        "--reason",
        "The accepted ranking is a stable adult nonfiction signal.",
        "--source-name",
        RANKING_SOURCE_NAME,
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
        "adult nonfiction",
        "--refresh-cadence",
        "weekly",
        "--url-stability",
        "stable category page",
        "--extraction-notes",
        "Metadata-only ranked titles and primary authors.",
        "--expected-page-heading",
        RANKING_HEADING,
        "--expected-signal-strength",
        "top ranking",
        "--eligible-candidate-category",
        "adult nonfiction",
        "--lifecycle-note",
        "Issue #401 active and reversible during probation.",
        "--notes",
        "Issue #401 accepted Source Proposal; replay-first onboarding.",
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
        RANKING_METADATA["ranking_evidence_url"],
        "--ranking-exclusion-term",
        RANKING_METADATA["adult_nonfiction_exclusions"][0],
        "--ranking-exclusion-term",
        RANKING_METADATA["adult_nonfiction_exclusions"][1],
        "--ranking-exclusion-term",
        RANKING_METADATA["adult_nonfiction_exclusions"][2],
        "--proposals-file",
        str(repo / "runs" / "current-weekly-pilot-russian" / "source-proposals.json"),
        "--portfolio-config",
        str(portfolio),
        "--pilot-config",
        str(configs[0]),
        "--pilot-live-config",
        str(configs[1]),
        "--live-config",
        str(configs[2]),
        "--repo-root",
        str(repo),
    ]
    _init_git(repo)
    return {"repo": repo, "evidence": evidence, "args": args, "configs": configs, "portfolio": portfolio}


def test_confirmed_ranking_onboarding_passes_the_exact_fixture_pair_guard(
    capsys: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = _ranking_case(tmp_path)
    assert main(case["args"]) == 3
    preview = json.loads(capsys.readouterr().out)
    runners = OnboardingRunners(
        replay=default_replay_runner,
        contracts=lambda *_args, **_kwargs: {"status": "passed"},
        full_suite=lambda *_args, **_kwargs: {"status": "passed"},
    )
    monkeypatch.setattr("autoscout.source_onboarding_transaction.default_onboarding_runners", lambda: runners)

    assert main([*case["args"], "--confirmation-token", preview["confirmation_token"], "--confirmed-by-editor"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["outcome"] == "completed"
    assert report["commit"]["committed"] is True
    assert (
        subprocess.run(
            ["git", "rev-list", "--count", "HEAD"], cwd=case["repo"], check=True, capture_output=True, text=True
        ).stdout.strip()
        == "2"
    )
    source = json.loads(case["portfolio"].read_text(encoding="utf-8"))["sources"][-1]
    for field in (
        "visible_title_selector",
        "entry_scope_selector",
        "ranking_source_statement",
        "ranking_expected_path",
    ):
        assert source[field] == RANKING_METADATA[field]
    replay_stage = next(stage for stage in report["stages"] if stage["stage"] == "replay")
    source_run = replay_stage["source_run"]
    assert source_run["status"] == "collected"
    assert source_run["raw_mention_count"] == 3
    assert source_run["valid_mention_count"] == 3
    assert source_run["skipped_item_count"] == 0
    page_path = next((case["repo"] / "data" / "current-weekly-pilot").glob("codex-capture-*.json"))
    extraction_path = next((case["repo"] / "data" / "current-weekly-pilot").glob("*-codex-extraction.json"))
    page_bytes = page_path.read_bytes()
    page = json.loads(page_bytes)
    extraction = json.loads(extraction_path.read_text(encoding="utf-8"))
    assert page["reference_date"] == "2026-08-10"
    assert extraction["capture_sha256"] == hashlib.sha256(page_bytes).hexdigest()
    rows = ranking_rows(page["html"], RANKING_METADATA)
    assert len(rows) == 20
    assert [row.rank for row in rows] == [str(rank) for rank in range(1, 21)]
    assert len({row.identifier for row in rows}) == 20
    mentions = extraction["mentions"]
    assert [int(mention["current_rank"]) for mention in mentions] == [2, 3, 4]
    assert len(mentions) == 3
    for mention in mentions:
        assert mention["candidate_category"] == "adult nonfiction"
        assert mention["source_url"] == RANKING_URL
        assert mention["evidence_url"] == RANKING_URL
        assert mention["search_translation"]
        assert mention["author_transliteration_note"]
        assert not {"description", "review", "sales_units", "promotional_prose"} & mention.keys()
    assert {row.identifier for row in rows if row.rank not in {"2", "3", "4"}}.isdisjoint(
        {mention["isbn"] for mention in mentions}
    )
    assert not any("Unselected ranking row" in mention["title"] for mention in mentions)
    assert not any("Omitted ranking row" in mention["title"] for mention in mentions)
    inspection_output = tmp_path / "post-confirm-replay"
    run_mvp(case["configs"][0], inspection_output, state_dir=tmp_path / "post-confirm-state")
    candidates = json.loads((inspection_output / "scouting-candidates.json").read_text(encoding="utf-8"))
    assert not any(
        str(candidate.get("title", "")).startswith(("Unselected ranking row", "Omitted ranking row"))
        for candidate in candidates
    )


def test_default_replay_runner_reports_bounded_source_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_root = tmp_path / "stage"
    stage_root.mkdir()

    def fake_run_mvp(_config_path: Path, output_dir: Path, *, state_dir: Path | None = None) -> None:
        assert state_dir == stage_root / "tmp" / "issue-339-replay-validation-state"
        output_dir.mkdir(parents=True)
        _write_json(
            output_dir / "source-runs.json",
            [
                {
                    "source_name": RANKING_SOURCE_NAME,
                    "status": "failed",
                    "reason": "failure at /private/secret/page.html <html>token",
                    "valid_mention_count": 0,
                    "skipped_item_count": 2,
                    "drop_reasons": [
                        {"reason": "ranking row is not grounded"},
                        {"reason": "/private/secret/raw.html"},
                    ],
                    "debug_metadata": {"safe_failure_status": "contract", "validated_mention_count": 0},
                }
            ],
        )

    monkeypatch.setattr("autoscout.cli.weekly.run_mvp", fake_run_mvp)
    result = default_replay_runner(
        stage_root,
        {"config_paths": SimpleNamespace(pilot=stage_root / "configs" / "pilot.json")},
        {"name": RANKING_SOURCE_NAME},
    )

    assert result["status"] == "failed"
    diagnostics = result["diagnostics"]
    assert diagnostics == {
        "source_run_status": "failed",
        "source_run_reason": "[redacted]",
        "valid_mention_count": 0,
        "skipped_item_count": 2,
        "safe_failure_status": "contract",
        "drop_reasons": ["ranking row is not grounded", "[redacted]"],
    }


def test_changed_retained_layout_field_blocks_confirmed_payload(capsys: Any, tmp_path: Path) -> None:
    case = _ranking_case(tmp_path)
    assert main(case["args"]) == 3
    preview = json.loads(capsys.readouterr().out)
    evidence = json.loads(case["evidence"].read_text(encoding="utf-8"))
    evidence["contract_metadata"]["ranking_source_statement"] = "A changed ranking statement."
    _write_json(case["evidence"], evidence)
    before_configs = [path.read_bytes() for path in case["configs"]]

    assert main([*case["args"], "--confirmation-token", preview["confirmation_token"], "--confirmed-by-editor"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "confirmation_payload_mismatch"
    assert [path.read_bytes() for path in case["configs"]] == before_configs
    assert not (case["repo"] / "runs" / "current-weekly-pilot-russian" / "source-proposals.json").exists()
    assert (
        subprocess.run(
            ["git", "rev-list", "--count", "HEAD"], cwd=case["repo"], check=True, capture_output=True, text=True
        ).stdout.strip()
        == "1"
    )
