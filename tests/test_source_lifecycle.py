from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from source_contract_battery import assert_source_onboarding_metadata
from state_store_support import seed_state
from test_source_onboarding_contract import LEGACY_PORTFOLIO_SOURCES


def run_autoscout(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "autoscout", *args], check=False, capture_output=True, text=True)


def write_config(path: Path, sources: list[dict], run_date: str = "2026-06-15", **extra: object) -> None:
    path.write_text(json.dumps({"run_date": run_date, "sources": sources, **extra}), encoding="utf-8")


def source(name: str, candidate: dict, *, lifecycle_status: str = "active") -> dict:
    return {
        "name": name,
        "family": "media/curated list",
        "url": f"https://example.test/{name.lower().replace(' ', '-')}",
        "access_status": "open",
        "extraction_method": "fixture",
        "check_frequency": "weekly",
        "language": "en",
        "lifecycle_status": lifecycle_status,
        "lifecycle_note": "new source under review" if lifecycle_status == "probation" else "",
        "fixture_candidates": [candidate],
    }


def test_source_metrics_artifact_and_review_table_include_probation(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    seed_state(
        state_dir,
        candidate_history=[
            {
                "identity_key": "shared book|alex example",
                "editor_status": "interested",
                "last_seen_date": "2026-06-10",
            },
            {
                "identity_key": "old book|olivia example",
                "editor_status": "interested",
                "last_seen_date": "2026-01-01",
                "source_evidence": [{"source_name": "Old Source", "source_family": "media/curated list"}],
            },
        ],
    )
    candidate = {
        "title": "Shared Book",
        "primary_authors": ["Alex Example"],
        "candidate_category": "history",
        "source_url": "https://example.test/shared",
        "signal_strength": "shortlist",
        "evidence_note": "Listed.",
        "russian_edition_status": "not found in Russian",
    }
    config_path = tmp_path / "sources.json"
    write_config(
        config_path,
        [
            source("Source One", candidate, lifecycle_status="probation"),
            source("Source Two", candidate),
            {
                "name": "Old Source",
                "family": "media/curated list",
                "url": "https://example.test/old-source",
                "access_status": "open",
                "extraction_method": "fixture",
                "check_frequency": "weekly",
                "language": "en",
                "fixture_candidates": [],
            },
        ],
        source_metrics_window_weeks=4,
    )
    output_dir = tmp_path / "run"

    result = run_autoscout(
        "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
    )

    assert result.returncode == 0, result.stdout + result.stderr
    metrics = json.loads((output_dir / "source-metrics.json").read_text(encoding="utf-8"))
    source_one = next(metric for metric in metrics["metrics"] if metric["source_name"] == "Source One")
    assert source_one["window_weeks"] == 4
    assert source_one["lifecycle_status"] == "probation"
    assert source_one["candidate_count"] == 1
    assert source_one["unique_contribution_count"] == 0
    assert source_one["corroborated_count"] == 1
    assert source_one["editor_yield_count"] == 1
    old_source = next(metric for metric in metrics["metrics"] if metric["source_name"] == "Old Source")
    assert old_source["candidate_count"] == 0
    review = (output_dir / "source-review.md").read_text(encoding="utf-8")
    assert "probation" in review
    assert "No source is added, retired, or promoted automatically" in review


def test_source_lifecycle_retirement_is_explicit_and_reversible(tmp_path: Path) -> None:
    config_path = tmp_path / "sources.json"
    write_config(
        config_path,
        [
            {
                "name": "Trial Source",
                "family": "media/curated list",
                "url": "https://example.test/trial",
                "access_status": "open",
                "extraction_method": "fixture",
                "check_frequency": "weekly",
                "language": "en",
                "fixture_candidates": [],
            }
        ],
    )

    retired = run_autoscout(
        "source-lifecycle",
        "retire",
        "--config",
        str(config_path),
        "--source-url",
        "https://example.test/trial",
        "--reason",
        "No useful candidates after review.",
    )

    assert retired.returncode == 0, retired.stdout + retired.stderr
    assert "automatic_lifecycle_change=false" in retired.stdout
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["sources"][0]["active"] is False
    assert config["sources"][0]["lifecycle_status"] == "retired"
    assert config["sources"][0]["retirement_reason"] == "No useful candidates after review."

    promoted = run_autoscout(
        "source-lifecycle",
        "promote",
        "--config",
        str(config_path),
        "--source-url",
        "https://example.test/trial",
        "--reason",
        "Editor wants to try again.",
    )

    assert promoted.returncode == 0, promoted.stdout + promoted.stderr
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["sources"][0]["active"] is True
    assert config["sources"][0]["lifecycle_status"] == "established"


def test_rejection_pattern_suggestions_are_inert_until_profile_entry_is_recorded(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    seed_state(
        state_dir,
        candidate_history=[
            {"identity_key": "crypto one|alex example", "editor_status": "reject"},
            {"identity_key": "crypto two|bea example", "editor_status": "reject"},
        ],
    )
    candidates = [
        {
            "title": "Crypto One",
            "primary_authors": ["Alex Example"],
            "candidate_category": "crypto",
            "source_url": "https://example.test/one",
            "signal_strength": "shortlist",
            "evidence_note": "Crypto list.",
            "russian_edition_status": "not found in Russian",
        },
        {
            "title": "Crypto Two",
            "primary_authors": ["Bea Example"],
            "candidate_category": "crypto",
            "source_url": "https://example.test/two",
            "signal_strength": "shortlist",
            "evidence_note": "Crypto list.",
            "russian_edition_status": "not found in Russian",
        },
    ]
    config_path = tmp_path / "sources.json"
    write_config(config_path, [source("Crypto Source", candidates[0] | {"title": "Crypto One"})])
    # Keep both same-source candidates in one configured source for deterministic pattern evidence.
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["sources"][0]["fixture_candidates"] = candidates
    config_path.write_text(json.dumps(config), encoding="utf-8")
    output_dir = tmp_path / "run"

    result = run_autoscout(
        "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
    )

    assert result.returncode == 0, result.stdout + result.stderr
    suggestions = json.loads((output_dir / "derived-rule-suggestions.json").read_text(encoding="utf-8"))
    topic_suggestion = next(s for s in suggestions["suggestions"] if s["scope"] == "topic")
    assert topic_suggestion["subject"] == "crypto"
    assert topic_suggestion["inert_until_promoted"] is True
    assert not (state_dir / "editorial-profile.json").exists()


PORTFOLIO_PATH = Path(__file__).resolve().parents[1] / "configs/current-weekly-portfolio.json"


def _first_non_legacy_source_index(sources: list[dict]) -> int:
    return next(index for index, entry in enumerate(sources) if entry["name"] not in LEGACY_PORTFOLIO_SOURCES)


@pytest.mark.parametrize(
    ("command", "expected_status", "expected_active"),
    [("promote", "established", True), ("retire", "retired", False), ("keep", "probation", True)],
)
def test_lifecycle_command_on_real_portfolio_changes_only_its_source_and_meets_the_contract(
    tmp_path: Path, command: str, expected_status: str, expected_active: bool
) -> None:
    """#1111: a lifecycle command keeps the hand-ordered config's key order and
    every untouched source byte-identical, and leaves the targeted source within
    the onboarding contract (promote records ``established``, not ``active``)."""
    portfolio_text = PORTFOLIO_PATH.read_text(encoding="utf-8")
    original = json.loads(portfolio_text)
    assert json.dumps(original, indent=2, ensure_ascii=False) + "\n" == portfolio_text
    first_source_keys = list(original["sources"][0])
    assert first_source_keys != sorted(first_source_keys), "the fixture must not be in alphabetical key order"
    # Put the target on probation in the copy so the test does not depend on
    # the portfolio still holding a probation source.
    target_index = _first_non_legacy_source_index(original["sources"])
    original["sources"][target_index]["lifecycle_status"] = "probation"
    original_text = json.dumps(original, indent=2, ensure_ascii=False) + "\n"
    target_name = original["sources"][target_index]["name"]
    config_path = tmp_path / "portfolio.json"
    config_path.write_text(original_text, encoding="utf-8")

    result = run_autoscout(
        "source-lifecycle",
        command,
        "--config",
        str(config_path),
        "--name",
        target_name,
        "--reason",
        "Brought a useful book.",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    updated_text = config_path.read_text(encoding="utf-8")
    updated = json.loads(updated_text)
    target = updated["sources"][target_index]
    assert target["name"] == target_name
    assert target["lifecycle_status"] == expected_status
    assert target["active"] is expected_active
    assert_source_onboarding_metadata(target)
    original_target = original["sources"][target_index]
    assert list(target)[: len(original_target)] == list(original_target)
    action = {"promote": "promoted", "retire": "retired", "keep": "kept"}[command]
    reason_key = "retirement_reason" if command == "retire" else f"{action}_reason"
    written_keys = {"active", "lifecycle_status", "lifecycle_note", reason_key, f"{action}_at", f"{action}_by"}
    assert {key: value for key, value in target.items() if key not in written_keys} == {
        key: value for key, value in original_target.items() if key not in written_keys
    }
    expected = dict(original)
    expected["sources"] = [*original["sources"][:target_index], target, *original["sources"][target_index + 1 :]]
    assert updated_text == json.dumps(expected, indent=2, ensure_ascii=False) + "\n"
