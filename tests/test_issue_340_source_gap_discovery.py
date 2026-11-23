"""Public-seam tests for Issue #340 source-gap discovery."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from autoscout.__main__ import main
from autoscout.search_optimization import SearchOptimizationReview
from autoscout.source_gap_discovery import CANONICAL_SIGNAL_FAMILIES
from autoscout.source_gap_discovery import DEFAULT_CAPTURE_DIR
from autoscout.source_gap_discovery import DEFAULT_FEASIBILITY_DIR
from autoscout.source_gap_discovery import DEFAULT_LEDGER_PATH
from autoscout.source_gap_discovery import SOURCE_GAP_SEED_CATALOG
from autoscout.source_gap_discovery import discover_source_gaps
from autoscout.source_gap_discovery import source_gap_error_report
from autoscout.source_probe import normalized_url

RATIONALE = "Для проверки доступности предложенных источников нужен сетевой доступ. Разрешить запуск?"


def _review() -> SearchOptimizationReview:
    return SearchOptimizationReview.construct(
        candidates=[],
        candidate_history=[],
        run_date="2026-08-10",
    )


def _review_with_allocations(*allocations: tuple[str, str, str, str]) -> SearchOptimizationReview:
    metrics: list[dict[str, Any]] = []
    for name, url, family, allocation in allocations:
        metrics.append(
            {
                "source_name": name,
                "source_url": url,
                "family": family,
                "active": True,
                "lifecycle_status": "active",
                "reviewed_candidate_count": 10,
                "interested_count": 2 if allocation == "keep" else 0,
                "needs_deeper_analysis_count": 0,
                "maybe_watch_count": 0,
                "already_known_count": 10 if allocation == "reduce" else 0,
                "reject_count": 0,
                "unique_contribution_rate": 0.0,
                "cross_family_corroboration_rate": 0.0,
            }
        )
    return SearchOptimizationReview.construct(
        candidates=[],
        candidate_history=[],
        run_date="2026-08-10",
        source_metrics={"metrics": metrics},
    )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _portfolio_source(
    name: str, family: str = "award", url: str = "https://configured.example/source", **extra: Any
) -> dict[str, Any]:
    return {"name": name, "family": family, "url": url, "active": True, "language": "en", **extra}


def test_report_has_one_simplified_schema_and_candidate_key() -> None:
    report = discover_source_gaps({"sources": []}, _review())

    assert set(report) == {
        "artifact_type",
        "version",
        "status",
        "requested_signal_family",
        "family_coverage",
        "sources_without_signal_family",
        "candidates",
        "ledger_exclusions",
        "probe",
        "next_action",
    }
    assert report["status"] == "success"
    assert report["version"] == 1
    assert report["sources_without_signal_family"] == []
    assert all("primary_gap_reason" not in coverage for coverage in report["family_coverage"].values())
    assert "proposed_candidates" not in report
    assert "artifact_version" not in report
    assert "schema_version" not in report
    assert "market_level_effect" not in report


def test_cli_outside_repo_ignores_caller_review_when_repository_default_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    portfolio_path = tmp_path / "portfolio.json"
    ledger_path = tmp_path / "ledger.json"
    _write_json(portfolio_path, {"sources": [_portfolio_source("Legacy source")]})
    caller_review_path = tmp_path / "runs/current-weekly-pilot-russian/search-optimization-review.json"
    caller_review_path.parent.mkdir(parents=True)
    caller_review_path.write_text("not the repository review", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    def fail_runner(_command: Any) -> tuple[int, str]:
        raise AssertionError("offline discovery invoked the probe runner")

    monkeypatch.setattr("autoscout.source_gap_discovery.run_probe_launcher", fail_runner)
    code = main(
        [
            "discover-source-gaps",
            "--portfolio",
            str(portfolio_path),
            "--ledger",
            str(ledger_path),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["status"] == "success"
    assert report["family_coverage"]["award"]["sources"][0]["allocation"] == "not-reviewed"
    assert report["family_coverage"]
    assert not ledger_path.exists()


def test_error_report_uses_explicit_error_kind() -> None:
    probe = {"status": "error", "command": ["./scripts/probe-source"], "exit_code": 2}

    default_report = source_gap_error_report("failure", probe=probe)
    probe_report = source_gap_error_report("failure", probe=probe, error_kind="probe_error")

    assert default_report["status"] == "input_error"
    assert default_report["sources_without_signal_family"] == []
    assert probe_report["status"] == "probe_error"
    assert "Inspect the probe failure" in probe_report["next_action"]


def test_explicit_missing_review_is_a_structured_input_error(tmp_path: Path, capsys: Any) -> None:
    portfolio_path = tmp_path / "portfolio.json"
    _write_json(portfolio_path, {"sources": []})

    code = main(
        [
            "discover-source-gaps",
            "--portfolio",
            str(portfolio_path),
            "--review",
            str(tmp_path / "missing-review.json"),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert report["status"] == "input_error"
    assert "missing-review.json" in report["error"]


def test_legacy_repository_configs_produce_reports() -> None:
    root = Path(__file__).resolve().parents[1]
    for filename in ("current-weekly-portfolio.json", "mvp-sources.example.json"):
        portfolio = json.loads((root / "configs" / filename).read_text(encoding="utf-8"))
        report = discover_source_gaps(portfolio, None, signal_family="award")
        assert report["status"] == "success"
        assert "award" in report["family_coverage"]


def test_unclassified_configured_url_is_silently_excluded() -> None:
    seed_url = "https://www.pulitzer.org/prize-winners-by-category/223/"
    portfolio = {
        "sources": [
            {"name": "Legacy search", "family": "topic search", "url": seed_url, "language": "en"},
            {"name": "Legacy metadata", "language": "en"},
        ]
    }

    report = discover_source_gaps(portfolio, None, signal_family="award")

    assert report["status"] == "insufficient_metadata"
    assert seed_url.rstrip("/") not in {item["url"].rstrip("/") for item in report["candidates"]}
    assert "configured_exclusions" not in report
    assert len(report["sources_without_signal_family"]) == 2


def test_absolute_input_live_cli_from_outside_repo_uses_repo_launcher_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    import autoscout.source_gap_discovery as discovery

    portfolio_path = tmp_path / "portfolio.json"
    review_path = tmp_path / "review.json"
    ledger_path = tmp_path / "ledger.json"
    _write_json(portfolio_path, {"sources": []})
    _write_json(review_path, _review().to_mapping())
    calls: list[dict[str, Any]] = []

    class Completed:
        returncode = 0
        stdout = json.dumps({"version": 1, "candidates": [], "exit_code": 0})

    def fake_run(*args: Any, **kwargs: Any) -> Completed:
        calls.append({"args": args, **kwargs})
        return Completed()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert (
        main(
            [
                "discover-source-gaps",
                "--portfolio",
                str(portfolio_path),
                "--review",
                str(review_path),
                "--ledger",
                str(ledger_path),
                "--probe",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert calls[0]["cwd"] == Path(discovery.__file__).resolve().parents[1]
    assert calls[0]["args"][0][0] == "./scripts/probe-source"


def test_probe_launcher_resolves_repository_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    import autoscout.source_gap_discovery as discovery

    calls: list[dict[str, Any]] = []

    class Completed:
        returncode = 0
        stdout = json.dumps({"version": 1, "candidates": [], "exit_code": 0})

    def fake_run(*args: Any, **kwargs: Any) -> Completed:
        calls.append({"args": args, **kwargs})
        return Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    discovery.run_probe_launcher(["./scripts/probe-source"])

    assert calls[0]["cwd"] == Path(discovery.__file__).resolve().parents[1]
    assert calls[0]["shell"] is False


def test_report_marks_one_active_source_as_weak() -> None:
    portfolio = {"sources": [_portfolio_source("Configured Awards")]}

    report = discover_source_gaps(portfolio, _review())

    assert report["sources_without_signal_family"] == []
    award = report["family_coverage"]["award"]

    assert award["configured_source_count"] == 1
    assert award["active_source_count"] == 1
    assert award["sources"][0]["allocation"] == "not-reviewed"
    assert award["weak"] is True
    assert award["weakness_reasons"] == ["one active source"]


def test_missing_family_metadata_is_explicit_and_suppresses_candidates() -> None:
    portfolio = {
        "sources": [
            _portfolio_source("Known Awards", url="https://configured.example/known"),
            {
                "name": "Unclassified",
                "url": "https://configured.example/unknown",
                "active": True,
                "language": "en",
            },
        ]
    }

    report = discover_source_gaps(portfolio, _review(), signal_family="award")

    assert report["status"] == "insufficient_metadata"
    assert report["sources_without_signal_family"] == [
        {
            "name": "Unclassified",
            "url": "https://configured.example/unknown",
            "active": True,
            "reason": "family metadata absent",
        }
    ]
    award = report["family_coverage"]["award"]
    assert award["configured_source_count"] == 1
    assert award["active_source_count"] == 1
    assert award["weak"] is None
    assert award["weakness_reasons"] == ["family metadata absent for 1 configured source"]
    assert report["candidates"] == []


def test_only_unsupported_families_are_insufficient_metadata() -> None:
    portfolio = {
        "sources": [
            _portfolio_source("Topic", family="topic search", url="https://configured.example/topic"),
            _portfolio_source("Country", family="country", url="https://configured.example/country"),
        ]
    }

    report = discover_source_gaps(portfolio, _review())

    assert report["status"] == "insufficient_metadata"
    assert [entry["name"] for entry in report["sources_without_signal_family"]] == ["Country", "Topic"]
    assert [entry["reason"] for entry in report["sources_without_signal_family"]] == [
        "unsupported signal family 'country'",
        "unsupported signal family 'topic search'",
    ]
    assert all(coverage["weak"] is None for coverage in report["family_coverage"].values())
    assert all(
        coverage["weakness_reasons"] == ["no configured source has a canonical signal family"]
        for coverage in report["family_coverage"].values()
    )
    assert report["candidates"] == []


def test_partial_canonical_and_unsupported_families_keep_canonical_analysis() -> None:
    portfolio = {
        "sources": [
            _portfolio_source("Awards", url="https://configured.example/awards"),
            _portfolio_source(
                "Topics",
                family="topic search",
                url="https://www.pulitzer.org/prize-winners-by-category/223",
            ),
        ]
    }

    report = discover_source_gaps(portfolio, _review(), signal_family="award")

    assert report["status"] == "success"
    assert report["sources_without_signal_family"][0]["name"] == "Topics"
    assert report["family_coverage"]["award"]["configured_source_count"] == 1
    assert report["family_coverage"]["award"]["weak"] is True
    assert "https://www.pulitzer.org/prize-winners-by-category/223" not in {
        candidate["url"] for candidate in report["candidates"]
    }


def test_empty_portfolio_remains_complete_weak_evidence() -> None:
    report = discover_source_gaps({"sources": []}, _review(), signal_family="award")

    assert report["status"] == "success"
    assert report["sources_without_signal_family"] == []
    assert report["family_coverage"]["award"]["weak"] is True
    assert report["candidates"]


def test_probe_insufficient_metadata_returns_zero_without_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fail_runner(command: Any) -> tuple[int, str]:
        calls.append(list(command))
        raise AssertionError("probe runner must not run")

    monkeypatch.setattr("autoscout.source_gap_discovery.run_probe_launcher", fail_runner)
    report = discover_source_gaps(
        {"sources": [{"name": "Unclassified", "url": "https://configured.example/unknown", "language": "en"}]},
        _review(),
        signal_family="award",
        probe=True,
    )

    assert report["status"] == "insufficient_metadata"
    assert report["probe"] == {"status": "no-candidates", "command": []}
    assert calls == []


def test_cli_probe_insufficient_metadata_returns_zero_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    portfolio_path = tmp_path / "portfolio.json"
    _write_json(
        portfolio_path,
        {"sources": [{"name": "Unclassified", "url": "https://configured.example/unknown", "language": "en"}]},
    )

    def fail_runner(_command: Any) -> tuple[int, str]:
        raise AssertionError("probe runner must not run")

    monkeypatch.setattr("autoscout.source_gap_discovery.run_probe_launcher", fail_runner)
    code = main(
        [
            "discover-source-gaps",
            "--portfolio",
            str(portfolio_path),
            "--signal-family",
            "award",
            "--probe",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["status"] == "insufficient_metadata"
    assert report["probe"]["status"] == "no-candidates"


def test_reduce_domination_uses_active_reviewed_labels_only() -> None:
    portfolio = {
        "sources": [
            _portfolio_source("Reduce One", url="https://configured.example/one"),
            _portfolio_source("Reduce Two", url="https://configured.example/two"),
            _portfolio_source("Inactive Keep", url="https://configured.example/inactive", active=False),
        ]
    }
    review = _review_with_allocations(
        ("Reduce One", "https://configured.example/one", "award", "reduce"),
        ("Reduce Two", "https://configured.example/two", "award", "reduce"),
        ("Inactive Keep", "https://configured.example/inactive", "award", "keep"),
    )

    award = discover_source_gaps(portfolio, review)["family_coverage"]["award"]

    assert [source["allocation"] for source in award["sources"]] == ["keep", "reduce", "reduce"]
    assert award["allocation_counts"]["keep"] == 1
    assert award["weakness_reasons"] == ["2 of 2 active reviewed sources have reduce allocation"]


def test_stale_same_name_review_row_is_not_reviewed() -> None:
    portfolio = {"sources": [_portfolio_source("Shared Identity", url="https://configured.example/current")]}
    stale_review = _review_with_allocations(
        ("Shared Identity", "https://configured.example/old", "award", "reduce"),
    )

    report = discover_source_gaps(portfolio, stale_review, signal_family="award")

    assert report["family_coverage"]["award"]["sources"][0]["allocation"] == "not-reviewed"


def test_stale_same_name_family_row_is_not_reviewed() -> None:
    portfolio = {
        "sources": [
            _portfolio_source(
                "Shared Identity",
                family="reader-interest",
                url="https://configured.example/current",
            )
        ]
    }
    stale_review = _review_with_allocations(
        ("Shared Identity", "https://configured.example/current", "award", "reduce"),
    )

    report = discover_source_gaps(portfolio, stale_review, signal_family="reader-interest")

    assert report["family_coverage"]["reader-interest"]["sources"][0]["allocation"] == "not-reviewed"


def test_candidate_generation_is_limited_to_computed_weak_families() -> None:
    portfolio = {
        "sources": [
            _portfolio_source("Awards One", url="https://configured.example/one"),
            _portfolio_source("Awards Two", url="https://configured.example/two"),
        ]
    }
    review = _review_with_allocations(
        ("Awards One", "https://configured.example/one", "award", "keep"),
        ("Awards Two", "https://configured.example/two", "award", "keep"),
    )

    report = discover_source_gaps(portfolio, review, signal_family="award")

    assert report["family_coverage"]["award"]["weak"] is False
    assert report["candidates"] == []


def test_every_weak_canonical_family_has_grounded_candidate_seeds() -> None:
    report = discover_source_gaps({"sources": []}, _review())

    expected = {
        "award": {
            "https://www.pulitzer.org/prize-winners-by-category/223",
            "https://royalsociety.org/medals-and-prizes/science-book-prize/",
        },
        "bestseller/ranking": {
            "https://www.casadellibro.com/libros-mas-vendidos",
            "https://www.books.com.tw/web/sys_saletopb/books/",
        },
        "media/curated list": {
            "https://www.mckinsey.com/featured-insights/mckinsey-on-books/author-talks",
            "https://www.scientificamerican.com/books/",
        },
        "reader-interest": {
            "https://app.thestorygraph.com/",
            "https://www.librarything.com/zeitgeist",
        },
        "audiobook ranking": {
            "https://www.audible.com/charts/best/money-finance-audiobooks/18574547011",
            "https://www.audible.com/charts/best/health-wellness-audiobooks/18573370011",
        },
        "rights/deal": {
            "https://www.publishersweekly.com/pw/by-topic/industry-news/book-deals/index.html",
        },
    }
    candidates_by_family = {
        family: {candidate["url"] for candidate in report["candidates"] if candidate["signal_family"] == family}
        for family in CANONICAL_SIGNAL_FAMILIES
    }

    assert candidates_by_family == expected
    assert all(candidate["reason"] for candidate in report["candidates"])


def test_candidate_reason_contains_computed_thin_gap_and_research_basis() -> None:
    report = discover_source_gaps({"sources": []}, _review(), signal_family="award")

    assert report["candidates"][0]["reason"] == (
        "Family gap: no active source. Research basis: Repository requirements list the Pulitzer General Nonfiction category."
    )


def test_candidate_reason_contains_computed_reduce_gap() -> None:
    portfolio = {
        "sources": [
            _portfolio_source("Reduce One", url="https://configured.example/one"),
            _portfolio_source("Reduce Two", url="https://configured.example/two"),
            _portfolio_source("Keep One", url="https://configured.example/keep"),
        ]
    }
    review = _review_with_allocations(
        ("Reduce One", "https://configured.example/one", "award", "reduce"),
        ("Reduce Two", "https://configured.example/two", "award", "reduce"),
        ("Keep One", "https://configured.example/keep", "award", "keep"),
    )

    report = discover_source_gaps(portfolio, review, signal_family="award")

    assert report["candidates"][0]["reason"] == (
        "Family gap: 2 of 3 active reviewed sources have reduce allocation. "
        "Research basis: Repository requirements list the Pulitzer General Nonfiction category."
    )


def test_probe_command_receives_the_same_dynamic_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_runner(command: Any) -> tuple[int, str]:
        calls.append(list(command))
        return 3, json.dumps({"version": 1, "candidates": [], "exit_code": 3})

    monkeypatch.setattr("autoscout.source_gap_discovery.run_probe_launcher", fake_runner)
    report = discover_source_gaps(
        {"sources": []},
        _review(),
        signal_family="award",
        ledger_identities=set(),
        ledger_path=Path("tmp/ledger.json"),
        probe=True,
    )

    command = calls[0]
    contexts = [candidate["reason"] for candidate in report["candidates"]]
    command_contexts = [command[index + 1] for index, value in enumerate(command) if value == "--proposing-context"]
    assert command_contexts == contexts
    assert command[-6:] == [
        "--keep-captures",
        str(DEFAULT_CAPTURE_DIR),
        "--ledger",
        "tmp/ledger.json",
        "--feasibility-dir",
        str(DEFAULT_FEASIBILITY_DIR),
    ]
    assert report["probe"]["status"] == "completed"
    assert report["probe"]["exit_code"] == 3
    assert report["probe"]["result"]["exit_code"] == 3
    assert report["status"] == "success"


def test_requested_family_filters_coverage_and_candidates() -> None:
    portfolio = {
        "sources": [
            _portfolio_source("Awards", url="https://configured.example/awards"),
            _portfolio_source("Reader", family="reader-interest", url="https://configured.example/reader"),
        ]
    }

    report = discover_source_gaps(portfolio, _review(), signal_family="award")

    assert list(report["family_coverage"]) == ["award"]
    assert all(candidate["signal_family"] == "award" for candidate in report["candidates"])
    assert report["requested_signal_family"] == "award"


def test_unsupported_requested_family_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported signal family"):
        discover_source_gaps({"sources": []}, _review(), signal_family="country")


def test_configured_seed_url_is_silently_omitted() -> None:
    configured_url = "https://www.pulitzer.org/prize-winners-by-category/223/"
    portfolio = {"sources": [_portfolio_source("Configured Pulitzer", url=configured_url)]}

    report = discover_source_gaps(portfolio, _review(), signal_family="award")

    configured_identity = normalized_url(configured_url)
    assert configured_identity not in {normalized_url(item["url"]) for item in report["candidates"]}
    assert "configured_exclusions" not in report


def test_normalized_ledger_seed_url_is_excluded_before_probe() -> None:
    ledger_url = "https://www.books.com.tw/web/sys_saletopb/books/"
    report = discover_source_gaps({"sources": []}, _review(), {normalized_url(ledger_url)})

    ledger_identity = normalized_url(ledger_url)
    assert ledger_identity in {normalized_url(item["url"]) for item in report["ledger_exclusions"]}
    assert ledger_identity not in {normalized_url(item["url"]) for item in report["candidates"]}
    assert report["probe"]["status"] == "planned"
    assert report["probe"]["command"][0] == "./scripts/probe-source"
    assert report["probe"]["command"][-6:] == [
        "--keep-captures",
        str(DEFAULT_CAPTURE_DIR),
        "--ledger",
        str(DEFAULT_LEDGER_PATH),
        "--feasibility-dir",
        str(DEFAULT_FEASIBILITY_DIR),
    ]


def test_missing_ledger_is_empty_and_report_bytes_are_deterministic() -> None:
    portfolio = {"sources": [_portfolio_source("Awards")]}
    first = json.dumps(discover_source_gaps(portfolio, _review()), sort_keys=True, ensure_ascii=False)
    second = json.dumps(discover_source_gaps(portfolio, _review()), sort_keys=True, ensure_ascii=False)

    assert first == second
    assert json.loads(first)["ledger_exclusions"] == []


def test_no_candidates_in_probe_mode_returns_zero_without_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    portfolio = {
        "sources": [
            _portfolio_source("Awards One", url="https://configured.example/one"),
            _portfolio_source("Awards Two", url="https://configured.example/two"),
        ]
    }
    review = _review_with_allocations(
        ("Awards One", "https://configured.example/one", "award", "keep"),
        ("Awards Two", "https://configured.example/two", "award", "keep"),
    )

    def fail_runner(_command: Any) -> tuple[int, str]:
        raise AssertionError("probe runner must not run")

    monkeypatch.setattr("autoscout.source_gap_discovery.run_probe_launcher", fail_runner)
    report = discover_source_gaps(portfolio, review, signal_family="award", probe=True)

    assert report["candidates"] == []
    assert report["probe"] == {"status": "no-candidates", "command": []}


@pytest.mark.parametrize("exit_code", [0, 1, 2, 3])
def test_cli_propagates_probe_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, exit_code: int
) -> None:
    portfolio_path = tmp_path / "portfolio.json"
    review_path = tmp_path / "review.json"
    ledger_path = tmp_path / "ledger.json"
    _write_json(portfolio_path, {"sources": []})
    _write_json(review_path, _review().to_mapping())
    probe_report = {"version": 1, "candidates": [], "exit_code": exit_code}
    calls: list[list[str]] = []

    def fake_runner(command: Any) -> tuple[int, str]:
        calls.append(list(command))
        return exit_code, json.dumps(probe_report)

    monkeypatch.setattr("autoscout.source_gap_discovery.run_probe_launcher", fake_runner)
    code = main(
        [
            "discover-source-gaps",
            "--portfolio",
            str(portfolio_path),
            "--review",
            str(review_path),
            "--ledger",
            str(ledger_path),
            "--signal-family",
            "award",
            "--probe",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert code == exit_code
    assert calls and calls[0][0] == "./scripts/probe-source"
    assert report["probe"]["result"] == probe_report
    assert report["status"] == ("probe_error" if exit_code == 2 else "success")


def test_cli_malformed_probe_output_returns_structured_code_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    portfolio_path = tmp_path / "portfolio.json"
    review_path = tmp_path / "review.json"
    _write_json(portfolio_path, {"sources": []})
    _write_json(review_path, _review().to_mapping())

    monkeypatch.setattr(
        "autoscout.source_gap_discovery.run_probe_launcher",
        lambda _command: (0, "not-json"),
    )
    code = main(
        [
            "discover-source-gaps",
            "--portfolio",
            str(portfolio_path),
            "--review",
            str(review_path),
            "--probe",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert report["status"] == "probe_error"
    assert report["probe"]["status"] == "error"
    assert report["probe"]["exit_code"] == 2
    assert report["probe"]["command"][0] == "./scripts/probe-source"


def test_probe_report_process_exit_mismatch_returns_structured_code_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    portfolio_path = tmp_path / "portfolio.json"
    review_path = tmp_path / "review.json"
    _write_json(portfolio_path, {"sources": []})
    _write_json(review_path, _review().to_mapping())

    monkeypatch.setattr(
        "autoscout.source_gap_discovery.run_probe_launcher",
        lambda _command: (0, json.dumps({"version": 1, "candidates": [], "exit_code": 3})),
    )
    code = main(
        [
            "discover-source-gaps",
            "--portfolio",
            str(portfolio_path),
            "--review",
            str(review_path),
            "--probe",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert report["status"] == "probe_error"
    assert report["probe"]["exit_code"] == 2
    assert "disagrees" in report["error"]


def test_probe_report_missing_process_exit_code_returns_structured_code_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    portfolio_path = tmp_path / "portfolio.json"
    review_path = tmp_path / "review.json"
    _write_json(portfolio_path, {"sources": []})
    _write_json(review_path, _review().to_mapping())

    monkeypatch.setattr(
        "autoscout.source_gap_discovery.run_probe_launcher",
        lambda _command: (0, json.dumps({"version": 1, "candidates": []})),
    )
    code = main(
        [
            "discover-source-gaps",
            "--portfolio",
            str(portfolio_path),
            "--review",
            str(review_path),
            "--probe",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert report["probe"]["exit_code"] == 2
    assert "valid exit_code" in report["error"]


def test_probe_runner_wrong_return_shape_returns_structured_code_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    portfolio_path = tmp_path / "portfolio.json"
    review_path = tmp_path / "review.json"
    _write_json(portfolio_path, {"sources": []})
    _write_json(review_path, _review().to_mapping())

    monkeypatch.setattr(
        "autoscout.source_gap_discovery.run_probe_launcher",
        lambda _command: object(),
    )
    code = main(
        [
            "discover-source-gaps",
            "--portfolio",
            str(portfolio_path),
            "--review",
            str(review_path),
            "--probe",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert report["probe"]["exit_code"] == 2
    assert "exactly (exit_code, stdout)" in report["error"]


def test_domain_validates_portfolio_once_for_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any) -> None:
    import autoscout.source_gap_discovery as discovery
    from autoscout.config import validate_config as repository_validate_config

    calls = 0

    def counting_validate_config(value: Any) -> list[str]:
        nonlocal calls
        calls += 1
        return repository_validate_config(value)

    monkeypatch.setattr(discovery, "validate_config", counting_validate_config)
    portfolio_path = tmp_path / "portfolio.json"
    review_path = tmp_path / "review.json"
    _write_json(portfolio_path, {"sources": []})
    _write_json(review_path, _review().to_mapping())

    assert (
        main(
            [
                "discover-source-gaps",
                "--portfolio",
                str(portfolio_path),
                "--review",
                str(review_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert calls == 1


def test_offline_next_action_defers_probe_and_onboarding() -> None:
    next_action = discover_source_gaps({"sources": []}, _review())["next_action"]

    assert "separate explicit editor request" in next_action
    assert "./scripts/add-source discover --probe" in next_action
    assert "exactly one accepted candidate" in next_action


def test_invalid_ledger_identity_shape_is_rejected() -> None:
    with pytest.raises(ValueError, match="ledger identities"):
        discover_source_gaps({"sources": []}, _review(), {"not-a-set": {}})


def test_cli_weekly_portfolio_resolution_error_is_structured(tmp_path: Path, capsys: Any) -> None:
    portfolio_path = tmp_path / "overlay.json"
    _write_json(
        portfolio_path,
        {"weekly_portfolio": "missing-portfolio.json", "sources": []},
    )

    code = main(["discover-source-gaps", "--portfolio", str(portfolio_path)])

    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert report["status"] == "input_error"
    assert report["sources_without_signal_family"] == []
    assert "weekly portfolio file not found" in report["error"]


def test_cli_invalid_unclassified_source_metadata_is_structured_input_error(tmp_path: Path, capsys: Any) -> None:
    portfolio_path = tmp_path / "portfolio.json"
    _write_json(
        portfolio_path,
        {
            "sources": [
                {
                    "name": "Unclassified",
                    "url": "https://configured.example/unknown",
                    "active": "yes",
                    "language": "en",
                }
            ]
        },
    )

    code = main(["discover-source-gaps", "--portfolio", str(portfolio_path)])

    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert report["status"] == "input_error"
    assert report["sources_without_signal_family"] == []
    assert "active" in report["error"]


def test_cli_invalid_direct_portfolio_uses_structured_error(tmp_path: Path, capsys: Any) -> None:
    portfolio_path = tmp_path / "portfolio.json"
    review_path = tmp_path / "review.json"
    _write_json(portfolio_path, {"sources": [], "unsupported": True})
    _write_json(review_path, _review().to_mapping())

    code = main(
        [
            "discover-source-gaps",
            "--portfolio",
            str(portfolio_path),
            "--review",
            str(review_path),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert report["status"] == "input_error"
    assert "portfolio validation failed" in report["error"]


def test_cli_offline_never_invokes_probe_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    portfolio_path = tmp_path / "portfolio.json"
    review_path = tmp_path / "review.json"
    ledger_path = tmp_path / "ledger.json"
    _write_json(portfolio_path, {"sources": []})
    _write_json(review_path, _review().to_mapping())
    before = {path: path.read_bytes() for path in (portfolio_path, review_path)}

    def fail_runner(_command: Any) -> tuple[int, str]:
        raise AssertionError("offline discovery invoked the probe runner")

    monkeypatch.setattr("autoscout.source_gap_discovery.run_probe_launcher", fail_runner)
    code = main(
        [
            "discover-source-gaps",
            "--portfolio",
            str(portfolio_path),
            "--review",
            str(review_path),
            "--ledger",
            str(ledger_path),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["probe"]["status"] == "planned"
    assert {path: path.read_bytes() for path in (portfolio_path, review_path)} == before
    assert not ledger_path.exists()


def test_valid_json_ledger_does_not_inspect_invalid_sidecar(tmp_path: Path, capsys: Any) -> None:
    portfolio_path = tmp_path / "portfolio.json"
    review_path = tmp_path / "review.json"
    ledger_path = tmp_path / "ledger.json"
    sidecar_path = ledger_path.with_suffix(".md")
    _write_json(portfolio_path, {"sources": []})
    _write_json(review_path, _review().to_mapping())
    _write_json(
        ledger_path,
        {"version": 2, "accept_count": 0, "defer_count": 0, "reject_count": 0, "total_entry_count": 0, "entries": []},
    )
    sidecar_path.symlink_to(tmp_path / "missing-protected-sidecar")

    code = main(
        [
            "discover-source-gaps",
            "--portfolio",
            str(portfolio_path),
            "--review",
            str(review_path),
            "--ledger",
            str(ledger_path),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["ledger_exclusions"] == []
    assert sidecar_path.is_symlink()


def test_offline_discovery_preserves_protected_repository_artifacts(tmp_path: Path, capsys: Any) -> None:
    portfolio_path = tmp_path / "portfolio.json"
    review_path = tmp_path / "review.json"
    ledger_path = tmp_path / "ledger.json"
    _write_json(portfolio_path, {"sources": []})
    _write_json(review_path, _review().to_mapping())
    protected = {
        name: tmp_path / name
        for name in (
            "current-weekly-portfolio.json",
            "configuration.json",
            "current-weekly-pilot.json",
            "source-proposals.json",
            "source-mentions.json",
            "scouting-candidates.json",
            "latest-scouting-candidates.json",
            "candidate-history.json",
            "workflow-log.json",
            "editorial-profile.json",
            "candidate-visibility.json",
            "pending-returned-editor-rows.json",
            "source-runs.json",
            "weekly-review.md",
            "weekly-review.html",
            "editor-review-guide.md",
        )
    }
    for path in protected.values():
        path.write_bytes(b"protected bytes\\n")
    before_bytes = {path: path.read_bytes() for path in protected.values()}
    before_names = {path.name for path in tmp_path.iterdir()}

    assert (
        main(
            [
                "discover-source-gaps",
                "--portfolio",
                str(portfolio_path),
                "--review",
                str(review_path),
                "--ledger",
                str(ledger_path),
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert {path: path.read_bytes() for path in protected.values()} == before_bytes
    assert {path.name for path in tmp_path.iterdir()} == before_names


def test_probe_mode_does_not_touch_protected_artifacts_with_fake_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    portfolio_path = tmp_path / "portfolio.json"
    review_path = tmp_path / "review.json"
    ledger_path = tmp_path / "probe" / "ledger.json"
    _write_json(portfolio_path, {"sources": []})
    _write_json(review_path, _review().to_mapping())
    protected = tmp_path / "source-proposals.json"
    protected.write_bytes(b"protected bytes\\n")

    monkeypatch.setattr(
        "autoscout.source_gap_discovery.run_probe_launcher",
        lambda _command: (0, json.dumps({"version": 1, "candidates": [], "exit_code": 0})),
    )
    code = main(
        [
            "discover-source-gaps",
            "--portfolio",
            str(portfolio_path),
            "--review",
            str(review_path),
            "--ledger",
            str(ledger_path),
            "--signal-family",
            "award",
            "--probe",
        ]
    )
    capsys.readouterr()

    assert code == 0
    assert protected.read_bytes() == b"protected bytes\\n"
    assert not ledger_path.exists()


def test_add_source_discover_launcher_passes_offline_and_probe_modes(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    scripts = tmp_path / "repo" / "scripts"
    scripts.mkdir(parents=True)
    launcher = scripts / "add-source"
    launcher.write_text((root / "scripts" / "add-source").read_text(encoding="utf-8"), encoding="utf-8")
    launcher.chmod(0o755)
    fake_python = tmp_path / "repo" / ".venv" / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text("#!/usr/bin/env bash\nprintf '%s ' \"$@\"\n", encoding="utf-8")
    fake_python.chmod(0o755)

    offline = subprocess.run(
        [str(launcher), "discover", "--signal-family", "award"],
        capture_output=True,
        text=True,
        check=False,
    )
    live = subprocess.run(
        [str(launcher), "discover", "--signal-family", "award", "--probe"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert offline.returncode == 0
    assert "-m autoscout discover-source-gaps --signal-family award" in offline.stdout
    assert "--probe" not in offline.stdout
    assert live.returncode == 0
    assert "-m autoscout discover-source-gaps --signal-family award --probe" in live.stdout
    assert "probe-source-candidate" not in live.stdout


def test_cli_resolves_real_weekly_overlay_configs_to_the_canonical_portfolio(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config_names = ("current-weekly-pilot.json", "current-weekly-live.json")

    def run(config_name: str, ledger_path: Path) -> dict[str, Any]:
        result = subprocess.run(
            [
                str(root / "scripts/add-source"),
                "discover",
                "--portfolio",
                str(root / "configs" / config_name),
                "--ledger",
                str(ledger_path),
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    canonical = run("current-weekly-portfolio.json", tmp_path / "canonical-ledger.json")
    for index, config_name in enumerate(config_names):
        overlay = run(config_name, tmp_path / f"overlay-{index}-ledger.json")
        assert overlay["status"] == canonical["status"] == "success"
        assert overlay["sources_without_signal_family"] == []
        assert {
            family: (
                value["configured_source_count"],
                value["active_source_count"],
                value["sources"],
            )
            for family, value in overlay["family_coverage"].items()
        } == {
            family: (
                value["configured_source_count"],
                value["active_source_count"],
                value["sources"],
            )
            for family, value in canonical["family_coverage"].items()
        }
        assert overlay["candidates"] == canonical["candidates"]
        assert overlay["ledger_exclusions"] == canonical["ledger_exclusions"]


def test_documented_discover_launcher_works_without_review_artifact() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [str(root / "scripts/add-source"), "discover"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["status"] == "success"
    assert report["family_coverage"]
    assert "error" not in report


def test_add_source_discover_rejects_url_without_probe_or_artifacts(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [str(root / "scripts/add-source"), "discover", "--url", "https://example.test/unsupported"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "scripts/probe-source" not in result.stdout + result.stderr
    assert list(tmp_path.iterdir()) == []


def test_catalog_excludes_known_stale_rejected_seeds() -> None:
    stale_urls = {
        "https://www.radiofrance.fr/franceinter",
        "https://prixfemina.org/",
        "https://augustpriset.se/",
        "https://www.brageprisen.no/",
        "https://nffo.no/",
        "https://kirjasaatio.fi/finlandia-palkinnot",
    }
    catalog_urls = {seed.url for seed in SOURCE_GAP_SEED_CATALOG}

    assert stale_urls.isdisjoint(catalog_urls)
