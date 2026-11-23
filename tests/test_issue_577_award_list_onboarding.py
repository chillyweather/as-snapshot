"""Confirmed offline onboarding of a bounded annual award list (Issue #577).

The retained evidence is synthetic.  No test here makes a network request.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from autoscout.__main__ import main
from autoscout.award_list import award_rows
from autoscout.config import load_validated_config
from autoscout.source_onboarding import OnboardingRunners
from autoscout.source_onboarding_transaction import default_replay_runner

URL = "https://example.test/2026-book-awards-shortlist/"
SOURCE_NAME = "Synthetic Book Awards 2026 General Nonfiction Shortlist"
HEADING = "2026 Book Awards Shortlist Announcement"
CATEGORY = "General Nonfiction Book of the Year"
DECLARATION: dict[str, Any] = {
    "contract_family": "award-list",
    "award_year": 2026,
    "award_category_heading": CATEGORY,
    "award_row_selector": "li",
    "award_row_format": "{title}, {author} ({ignored})",
    "award_expected_row_count": 4,
    "award_author_conjunction": "and",
}
ROWS = (
    ("The Long Coast", "Mara Ellison", "Длинный берег", "Мара Эллисон"),
    ("Salt and Iron", "Daniel Mercer / Ines Okoro", "Соль и железо", "Дэниел Мерсер и Инес Окоро"),
    ("A Quiet Ledger", "Tomas Reyes", "Тихая книга учёта", "Томас Рейес"),
    ("The Fourth Row", "Unselected Author", "", ""),
)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _init_git(repo: Path) -> None:
    for args in (
        ("init", "-q"),
        ("config", "user.name", "Test"),
        ("config", "user.email", "test@example.test"),
        ("add", "."),
        ("commit", "-qm", "initial"),
    ):
        result = subprocess.run(["git", *args], cwd=repo, check=False, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def _award_case(tmp_path: Path) -> dict[str, Any]:
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
            {"run_date": "2026-10-11", "weekly_portfolio": portfolio.name, "sources": [{"name": "Existing"}]},
        )
    rows = "".join(
        f"<li>{title}, {author.replace(' / ', ' and ')} (Harbour Press)</li>" for title, author, _ru, _note in ROWS
    )
    evidence = repo / "data" / "source-candidate-captures" / "award-retained.json"
    _write_json(
        evidence,
        {
            "collection_mode": "source-candidate-probe",
            "source_url": URL,
            "source_page": URL,
            "resolved_url": URL,
            "head_status": 200,
            "get_status": 200,
            "reference_date": "2026-10-11",
            "html": (
                f"<!doctype html><html><body><h1>{HEADING}</h1>"
                "<h2>Biography Book of the Year</h2><ul><li>Another Life, Someone Else (Other House)</li></ul>"
                f"<h2>{CATEGORY}</h2><ul>{rows}</ul></body></html>"
            ),
            "contract_family": "award-list",
            "contract_metadata": DECLARATION,
            "source_name": SOURCE_NAME,
            "signal_family": "award",
        },
    )
    helper = repo / "helper.json"
    _write_json(
        helper,
        {
            "version": 1,
            "source_url": URL,
            "language": "en",
            "entries": [
                {
                    "title": title,
                    "author": author,
                    "evidence_url": URL,
                    "english_title": title,
                    "search_translation": translation,
                    "author_transliteration_note": transliteration,
                }
                for title, author, translation, transliteration in ROWS[:3]
            ],
        },
    )
    feasibility = repo / "feasibility-draft.md"
    feasibility.write_text(
        f"# Issue #577 feasibility evidence\n\nSource: {SOURCE_NAME}\n\nURL: {URL}\n\n"
        "The retained evidence confirms a bounded annual award list.\n",
        encoding="utf-8",
    )
    args = [
        "add-source",
        "--url",
        URL,
        "--probe-evidence",
        str(evidence),
        "--helper-metadata",
        str(helper),
        "--feasibility-evidence",
        str(feasibility),
        "--feasibility-doc",
        str(repo / "docs" / "issue-577-award-source-feasibility.md"),
        "--onboarding-issue",
        "577",
        "--run-date",
        "2026-10-11",
        "--reason",
        "The accepted shortlist is a bounded adult nonfiction award signal.",
        "--source-name",
        SOURCE_NAME,
        "--family",
        "award",
        "--signal-family",
        "award",
        "--access-status",
        "open",
        "--check-frequency",
        "annual",
        "--language",
        "en",
        "--country",
        "Australia",
        "--market",
        "Australia",
        "--expected-category",
        CATEGORY,
        "--refresh-cadence",
        "annual",
        "--url-stability",
        "year-specific",
        "--extraction-notes",
        "Metadata-only titles and primary authors of the bounded category rows.",
        "--expected-page-heading",
        HEADING,
        "--expected-signal-strength",
        "shortlist",
        "--eligible-candidate-category",
        "adult nonfiction",
        "--lifecycle-note",
        "Issue #577 active and reversible during probation.",
        "--notes",
        "Issue #577 accepted Source Proposal; replay-first onboarding.",
        "--contract-family",
        "award-list",
        "--award-year",
        "2026",
        "--award-category-heading",
        CATEGORY,
        "--award-row-selector",
        "li",
        "--award-row-format",
        DECLARATION["award_row_format"],
        "--award-expected-row-count",
        "4",
        "--award-author-conjunction",
        "and",
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
    return {"repo": repo, "args": args, "configs": configs, "portfolio": portfolio}


def test_confirmed_award_list_onboarding_configures_and_replays_the_source(
    capsys: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = _award_case(tmp_path)
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

    source = json.loads(case["portfolio"].read_text(encoding="utf-8"))["sources"][-1]
    assert {field: source[field] for field in DECLARATION} == DECLARATION
    assert source["check_frequency"] == "annual"
    assert "metadata_exact_book_list" not in source
    assert "metadata_ranking_table" not in source
    assert not any(field.startswith("ranking_") or field == "adult_nonfiction_exclusions" for field in source)

    source_run = next(stage for stage in report["stages"] if stage["stage"] == "replay")["source_run"]
    assert source_run["status"] == "collected"
    assert source_run["raw_mention_count"] == 3
    assert source_run["valid_mention_count"] == 3

    page_path = next((case["repo"] / "data" / "current-weekly-pilot").glob("codex-capture-*.json"))
    extraction_path = next((case["repo"] / "data" / "current-weekly-pilot").glob("*-codex-extraction.json"))
    page = json.loads(page_path.read_text(encoding="utf-8"))
    assert page["reference_date"] == "2026-10-11"
    assert [row.title for row in award_rows(page["html"], DECLARATION)] == [row[0] for row in ROWS]
    assert "Another Life" not in page["html"]
    mentions = json.loads(extraction_path.read_text(encoding="utf-8"))["mentions"]
    assert mentions[1]["primary_authors"] == ["Daniel Mercer", "Ines Okoro"]
    for mention in mentions:
        assert mention["source_url"] == mention["evidence_url"] == URL
        assert not {"current_rank", "isbn", "source_week"} & mention.keys()
    for config in case["configs"]:
        load_validated_config(config)
