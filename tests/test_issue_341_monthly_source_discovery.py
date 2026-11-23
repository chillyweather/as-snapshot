"""Deterministic public-CLI coverage for Issue #341 monthly discovery."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from state_store_support import seed_state
from state_store_support import state_databases
from state_store_support import sync_state

from autoscout.__main__ import main
from autoscout.monthly_source_discovery_artifacts import json_text
from autoscout.monthly_source_discovery_artifacts import monthly_evidence_digest
from autoscout.monthly_source_discovery_artifacts import monthly_operation_identity
from autoscout.monthly_source_discovery_artifacts import render_monthly_markdown
from autoscout.source_gap_discovery import SOURCE_GAP_SEED_CATALOG
from autoscout.source_probe import normalized_url


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _portfolio(path: Path, *, all_families: bool = False, unclassified: bool = False) -> None:
    if unclassified:
        sources: list[dict[str, Any]] = [
            {"name": "Unclassified", "url": "https://configured.example/unknown", "language": "en"}
        ]
    elif all_families:
        families = sorted({seed.signal_family for seed in SOURCE_GAP_SEED_CATALOG})
        sources = [
            {
                "name": f"{family} {index}",
                "family": family,
                "url": f"https://configured.example/{index}-{family.replace('/', '-').replace(' ', '-')}",
                "active": True,
                "language": "en",
            }
            for family in families
            for index in (1, 2)
        ]
    else:
        sources = []
    _write_json(path, {"sources": sources})


def _ledger_entry(url: str, outcome: str) -> dict[str, Any]:
    return {
        "normalized_url": normalized_url(url),
        "probe_date": "2026-08-01",
        "url": url,
        "name": "Ledger source",
        "signal_family": "award",
        "outcome": outcome,
        "reason_code": "test",
        "reason": "deterministic fixture",
        "http_evidence": {"head": {"status": 200, "resolved_url": url}, "get": {"status": 200, "resolved_url": url}},
        "head_status": 200,
        "get_status": 200,
        "resolved_url": url,
        "detected_heading": "Test",
        "bounded_entry_count": 1,
        "samples": [],
        "proposing_context": "Issue #341 test",
    }


def _ledger(path: Path, entries: list[dict[str, Any]]) -> None:
    counts = {"accept": 0, "defer": 0, "reject": 0}
    for entry in entries:
        counts[entry["outcome"]] += 1
    _write_json(
        path,
        {
            "version": 2,
            "accept_count": counts["accept"],
            "defer_count": counts["defer"],
            "reject_count": counts["reject"],
            "total_entry_count": len(entries),
            "entries": entries,
        },
    )


def _args(
    tmp_path: Path,
    month: str | None,
    *,
    portfolio: Path | None = None,
    output_dir: Path | None = None,
    ledger: Path | None = None,
    source_proposals: Path | None = None,
) -> list[str]:
    args = [
        "monthly-source-discovery",
        "--portfolio",
        str(portfolio or tmp_path / "portfolio.json"),
        "--ledger",
        str(ledger or tmp_path / "ledger.json"),
        "--source-proposals",
        str(source_proposals or tmp_path / "source-proposals.json"),
        "--output-dir",
        str(output_dir or tmp_path / "monthly"),
    ]
    if month is not None:
        args.extend(["--operation-month", month])
    return args


def _run(
    tmp_path: Path,
    capsys: Any,
    month: str | None = "2026-08",
    *,
    portfolio: Path | None = None,
    output_dir: Path | None = None,
    ledger: Path | None = None,
    source_proposals: Path | None = None,
) -> tuple[int, dict[str, Any]]:
    code = main(
        _args(
            tmp_path,
            month,
            portfolio=portfolio,
            output_dir=output_dir,
            ledger=ledger,
            source_proposals=source_proposals,
        )
    )
    return code, json.loads(capsys.readouterr().out)


def _run_launcher(
    tmp_path: Path,
    month: str,
    *,
    portfolio: Path | None = None,
    output_dir: Path | None = None,
    ledger: Path | None = None,
    source_proposals: Path | None = None,
) -> tuple[int, dict[str, Any]]:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            str(root / "scripts/add-source"),
            "monthly",
            "--operation-month",
            month,
            "--portfolio",
            str(portfolio or tmp_path / "portfolio.json"),
            "--ledger",
            str(ledger or tmp_path / "ledger.json"),
            "--source-proposals",
            str(source_proposals or tmp_path / "source-proposals.json"),
            "--output-dir",
            str(output_dir or tmp_path / "monthly"),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, json.loads(result.stdout)


def _report_path(tmp_path: Path, month: str) -> Path:
    return tmp_path / "monthly/reports" / month / "monthly-source-discovery.json"


def _markdown_path(tmp_path: Path, month: str) -> Path:
    return tmp_path / "monthly/reports" / month / "monthly-source-discovery.md"


def _rewrite_report_and_history(tmp_path: Path, mutate: Any) -> None:
    month = "2026-08"
    report_path = _report_path(tmp_path, month)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    mutate(report)
    report["evidence_digest"] = monthly_evidence_digest(report)
    report["operation_identity"] = monthly_operation_identity(month, report["evidence_digest"])
    report_text = json_text(report).encode("utf-8")
    markdown_text = render_monthly_markdown(report).encode("utf-8")
    report_path.write_bytes(report_text)
    _markdown_path(tmp_path, month).write_bytes(markdown_text)

    history_path = tmp_path / "monthly/monthly-source-discovery-history.json"
    history = json.loads(history_path.read_text(encoding="utf-8"))
    entry = next(item for item in history["entries"] if item["operation_month"] == month)
    entry["operation_identity"] = report["operation_identity"]
    entry["evidence_digest"] = report["evidence_digest"]
    entry["report_sha256"] = hashlib.sha256(report_text).hexdigest()
    entry["markdown_sha256"] = hashlib.sha256(markdown_text).hexdigest()
    _write_json(history_path, history)


@pytest.fixture(autouse=True)
def _monthly_route_must_not_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_runner(_command: Any) -> tuple[int, str]:
        raise AssertionError("monthly offline discovery attempted to invoke the probe runner")

    monkeypatch.setattr("autoscout.source_gap_discovery.run_probe_launcher", fail_runner)


def test_new_candidates_are_published_as_a_per_month_json_markdown_pair(tmp_path: Path, capsys: Any) -> None:
    _portfolio(tmp_path / "portfolio.json")
    seed_state(tmp_path, source_proposals=[])

    code, report = _run(tmp_path, capsys)

    assert code == 0
    assert report["status"] == "new_candidates_available"
    assert report["new_candidate_urls"]
    assert report["banked_candidate_urls"] == []
    assert report["ledger_rejected_or_deferred_urls"] == []
    assert json.loads(_report_path(tmp_path, "2026-08").read_text(encoding="utf-8")) == report
    markdown = _markdown_path(tmp_path, "2026-08").read_text(encoding="utf-8")
    assert report["status"] in markdown
    assert all(url in markdown for url in report["new_candidate_urls"])
    assert str(report["onboarding_progress"]["verified_completed_count"]) in markdown
    assert report["next_action"]


def test_monthly_history_does_not_bank_candidates(tmp_path: Path, capsys: Any) -> None:
    _portfolio(tmp_path / "portfolio.json")
    seed_state(tmp_path, source_proposals=[])
    accepted_url = SOURCE_GAP_SEED_CATALOG[0].url
    rejected_url = SOURCE_GAP_SEED_CATALOG[1].url
    _ledger(
        tmp_path / "ledger.json",
        [_ledger_entry(accepted_url, "accept"), _ledger_entry(rejected_url, "defer")],
    )

    first_code, first = _run(tmp_path, capsys, "2026-07")
    second_code, second = _run(tmp_path, capsys, "2026-08")

    assert first_code == second_code == 0
    assert accepted_url.rstrip("/") in {url.rstrip("/") for url in first["banked_candidate_urls"]}
    assert normalized_url(rejected_url) in second["ledger_rejected_or_deferred_urls"]
    assert accepted_url.rstrip("/") in {url.rstrip("/") for url in second["banked_candidate_urls"]}
    assert second["new_candidate_urls"]
    assert second["status"] == "new_candidates_available"
    history = json.loads((tmp_path / "monthly/monthly-source-discovery-history.json").read_text(encoding="utf-8"))
    assert [entry["operation_month"] for entry in history["entries"]] == ["2026-07", "2026-08"]


def test_full_ledger_banks_accepts_and_reports_all_rejects_and_defers(tmp_path: Path, capsys: Any) -> None:
    _portfolio(tmp_path / "portfolio.json")
    seed_state(tmp_path, source_proposals=[])
    accepted_url = "https://ledger.example/accepted"
    rejected_seed_url = SOURCE_GAP_SEED_CATALOG[0].url
    deferred_seed_url = SOURCE_GAP_SEED_CATALOG[1].url
    rejected_extra_url = "https://ledger.example/rejected"
    deferred_extra_url = "https://ledger.example/deferred"
    _ledger(
        tmp_path / "ledger.json",
        [
            _ledger_entry(accepted_url, "accept"),
            _ledger_entry(rejected_seed_url, "reject"),
            _ledger_entry(deferred_seed_url, "defer"),
            _ledger_entry(rejected_extra_url, "reject"),
            _ledger_entry(deferred_extra_url, "defer"),
        ],
    )

    code, report = _run(tmp_path, capsys)

    assert code == 0
    assert normalized_url(accepted_url) in report["banked_candidate_urls"]
    assert report["banked_candidate_urls"]
    assert report["ledger_rejected_or_deferred_urls"] == sorted(
        normalized_url(url) for url in (rejected_seed_url, deferred_seed_url, rejected_extra_url, deferred_extra_url)
    )


def test_configured_accepted_ledger_url_is_not_banked(tmp_path: Path, capsys: Any) -> None:
    portfolio = tmp_path / "portfolio.json"
    configured_url = "https://configured.example/accepted"
    _write_json(
        portfolio,
        {
            "sources": [
                {
                    "name": "Configured",
                    "family": "award",
                    "url": configured_url,
                    "active": True,
                    "language": "en",
                },
            ]
        },
    )
    seed_state(tmp_path, source_proposals=[])
    _ledger(tmp_path / "ledger.json", [_ledger_entry(configured_url, "accept")])

    code, report = _run(tmp_path, capsys, portfolio=portfolio)

    assert code == 0
    assert normalized_url(configured_url) not in report["banked_candidate_urls"]


def test_quiet_month_and_insufficient_metadata_are_nonfailures(tmp_path: Path, capsys: Any) -> None:
    quiet_portfolio = tmp_path / "quiet-portfolio.json"
    _portfolio(quiet_portfolio, all_families=True)
    seed_state(tmp_path, source_proposals=[])
    code, quiet = _run(tmp_path, capsys, "2026-08", portfolio=quiet_portfolio)
    assert code == 0
    assert quiet["status"] == "no_candidates_this_month"
    assert quiet["new_candidate_urls"] == []
    assert quiet["banked_candidate_urls"] == []

    insufficient_portfolio = tmp_path / "insufficient.json"
    _portfolio(insufficient_portfolio, unclassified=True)
    code, insufficient = _run(tmp_path, capsys, "2026-09", portfolio=insufficient_portfolio)
    assert code == 0
    assert insufficient["status"] == "insufficient_portfolio_metadata"
    assert insufficient["coverage_gaps"] == []
    assert insufficient["next_action"]


def test_insufficient_metadata_with_new_candidates_fails_closed_on_retry(tmp_path: Path, capsys: Any) -> None:
    portfolio = tmp_path / "insufficient.json"
    _portfolio(portfolio, unclassified=True)
    seed_state(tmp_path, source_proposals=[])
    assert _run(tmp_path, capsys, portfolio=portfolio)[0] == 0

    def mutate(report: dict[str, Any]) -> None:
        report["new_candidate_urls"] = ["https://tampered.example/source"]

    _rewrite_report_and_history(tmp_path, mutate)
    code, result = _run(tmp_path, capsys, portfolio=portfolio)

    assert code == 2
    assert result["status"] == "artifact_error"
    assert "despite insufficient" in result["error"]


def test_insufficient_metadata_preserves_banked_ledger_evidence_through_public_launcher(
    tmp_path: Path,
) -> None:
    portfolio = tmp_path / "insufficient.json"
    _portfolio(portfolio, unclassified=True)
    seed_state(tmp_path, source_proposals=[])
    accepted_url = "https://ledger.example/accepted"
    _ledger(tmp_path / "ledger.json", [_ledger_entry(accepted_url, "accept")])
    output_dir = tmp_path / "monthly"

    first_code, first_report = _run_launcher(
        tmp_path,
        "2026-08",
        portfolio=portfolio,
        output_dir=output_dir,
    )

    assert first_code == 0
    assert first_report["source_gap_status"] == "insufficient_metadata"
    assert first_report["status"] == "insufficient_portfolio_metadata"
    assert first_report["new_candidate_urls"] == []
    assert first_report["coverage_gaps"] == []
    assert first_report["banked_candidate_urls"] == [normalized_url(accepted_url)]
    report_path = _report_path(tmp_path, "2026-08")
    markdown_path = _markdown_path(tmp_path, "2026-08")
    history_path = output_dir / "monthly-source-discovery-history.json"
    first_bytes = {path: path.read_bytes() for path in (report_path, markdown_path, history_path)}
    report_json = json.loads(first_bytes[report_path].decode("utf-8"))
    assert report_json["banked_candidate_urls"] == [normalized_url(accepted_url)]
    assert normalized_url(accepted_url) in first_bytes[markdown_path].decode("utf-8")

    retry_code, retry_report = _run_launcher(
        tmp_path,
        "2026-08",
        portfolio=portfolio,
        output_dir=output_dir,
    )

    assert retry_code == 0
    assert retry_report == first_report
    assert {path: path.read_bytes() for path in first_bytes} == first_bytes
    history = json.loads(first_bytes[history_path].decode("utf-8"))
    assert [entry["operation_month"] for entry in history["entries"]] == ["2026-08"]
    assert history["entries"][0]["banked_candidate_urls"] == [normalized_url(accepted_url)]


def test_invalid_month_and_partial_publication_fail_closed(tmp_path: Path, capsys: Any) -> None:
    _portfolio(tmp_path / "portfolio.json")
    seed_state(tmp_path, source_proposals=[])
    code, invalid = _run(tmp_path, capsys, None)
    assert code == 2
    assert invalid["status"] == "input_error"
    assert invalid["next_action"]

    code, _ = _run(tmp_path, capsys, "2026-08")
    assert code == 0
    report_path = _report_path(tmp_path, "2026-08")
    markdown_path = _markdown_path(tmp_path, "2026-08")
    original_report = report_path.read_bytes()
    markdown_path.unlink()
    code, partial = _run(tmp_path, capsys, "2026-09")
    assert code == 2
    assert partial["status"] == "artifact_error"
    assert report_path.read_bytes() == original_report


def test_old_month_retry_after_newer_month_is_byte_identical_and_deduplicated(tmp_path: Path, capsys: Any) -> None:
    _portfolio(tmp_path / "portfolio.json")
    seed_state(tmp_path, source_proposals=[])
    code, july = _run(tmp_path, capsys, "2026-07")
    assert code == 0
    july_json = _report_path(tmp_path, "2026-07").read_bytes()
    july_md = _markdown_path(tmp_path, "2026-07").read_bytes()
    history_before_retry = (tmp_path / "monthly/monthly-source-discovery-history.json").read_bytes()

    assert _run(tmp_path, capsys, "2026-08")[0] == 0
    assert _run(tmp_path, capsys, "2026-09")[0] == 0
    code, retry = _run(tmp_path, capsys, "2026-07")

    assert code == 0
    assert retry == july
    assert _report_path(tmp_path, "2026-07").read_bytes() == july_json
    assert _markdown_path(tmp_path, "2026-07").read_bytes() == july_md
    history_after_retry = json.loads((tmp_path / "monthly/monthly-source-discovery-history.json").read_text())
    assert [entry["operation_month"] for entry in history_after_retry["entries"]] == ["2026-07", "2026-08", "2026-09"]
    assert history_before_retry != (tmp_path / "monthly/monthly-source-discovery-history.json").read_bytes()


def test_monthly_history_does_not_change_later_digest_or_report(tmp_path: Path) -> None:
    _portfolio(tmp_path / "portfolio.json")
    seed_state(tmp_path, source_proposals=[])
    clean = tmp_path / "clean"
    history_run = tmp_path / "history-run"
    assert _run_launcher(tmp_path, "2026-08", output_dir=clean)[0] == 0
    assert _run_launcher(tmp_path, "2026-07", output_dir=history_run)[0] == 0
    assert _run_launcher(tmp_path, "2026-08", output_dir=history_run)[0] == 0
    clean_report = json.loads((clean / "reports/2026-08/monthly-source-discovery.json").read_text())
    history_report = json.loads((history_run / "reports/2026-08/monthly-source-discovery.json").read_text())
    assert clean_report == history_report
    assert (clean / "reports/2026-08/monthly-source-discovery.md").read_bytes() == (
        history_run / "reports/2026-08/monthly-source-discovery.md"
    ).read_bytes()

    july_path = history_run / "reports/2026-07/monthly-source-discovery.json"
    july_before = july_path.read_bytes()
    july_model = json.loads(july_before)
    assert _run_launcher(tmp_path, "2026-07", output_dir=history_run)[0] == 0
    assert july_path.read_bytes() == july_before
    assert json.loads(july_path.read_text())["operation_identity"] == july_model["operation_identity"]


def test_public_launcher_retry_allows_date_order_opposite_to_url_order(tmp_path: Path) -> None:
    portfolio = tmp_path / "portfolio.json"
    first_url = "https://a.example/source"
    second_url = "https://z.example/source"
    _write_json(
        portfolio,
        {
            "sources": [
                {"name": "A source", "family": "award", "url": first_url, "active": True, "language": "en"},
                {"name": "Z source", "family": "award", "url": second_url, "active": True, "language": "en"},
            ]
        },
    )
    proposals = tmp_path / "source-proposals.json"
    _write_json(
        proposals,
        [
            {
                "id": "proposal-a",
                "source_url": first_url,
                "status": "accepted",
                "proposal_date": "2026-08-02",
                "onboarding_request": {"url": first_url, "run_date": "2026-08-03"},
            },
            {
                "id": "proposal-z",
                "source_url": second_url,
                "status": "accepted",
                "proposal_date": "2026-07-02",
                "onboarding_request": {"url": second_url, "run_date": "2026-07-03"},
            },
        ],
    )
    sync_state(proposals.parent)
    output_dir = tmp_path / "monthly"

    first_code, first_report = _run_launcher(
        tmp_path,
        "2026-08",
        portfolio=portfolio,
        source_proposals=proposals,
        output_dir=output_dir,
    )
    assert first_code == 0
    progress = first_report["onboarding_progress"]
    assert [source["url"] for source in progress["verified_completed_sources"]] == [second_url, first_url]
    assert [source["url"] for source in progress["accepted_proposal_signal"]["in_window_sources"]] == [
        second_url,
        first_url,
    ]

    report_path = _report_path(tmp_path, "2026-08")
    markdown_path = _markdown_path(tmp_path, "2026-08")
    history_path = output_dir / "monthly-source-discovery-history.json"
    before_retry = {path: path.read_bytes() for path in (report_path, markdown_path, history_path)}

    retry_code, retry_report = _run_launcher(
        tmp_path,
        "2026-08",
        portfolio=portfolio,
        source_proposals=proposals,
        output_dir=output_dir,
    )

    assert retry_code == 0
    assert retry_report == first_report
    assert {path: path.read_bytes() for path in (report_path, markdown_path, history_path)} == before_retry


def test_out_of_order_backfill_uses_only_earlier_history(tmp_path: Path, capsys: Any) -> None:
    _portfolio(tmp_path / "portfolio.json")
    seed_state(tmp_path, source_proposals=[])
    assert _run(tmp_path, capsys, "2026-08")[0] == 0
    code, july = _run(tmp_path, capsys, "2026-07")
    assert code == 0
    assert july["status"] == "new_candidates_available"
    assert july["new_candidate_urls"]
    assert july["banked_candidate_urls"] == []


def test_same_month_ledger_semantic_change_replaces_pair_and_one_history_entry(tmp_path: Path) -> None:
    portfolio = tmp_path / "portfolio.json"
    _portfolio(portfolio)
    proposals = tmp_path / "source-proposals.json"
    ledger = tmp_path / "ledger.json"
    _write_json(proposals, [])
    sync_state(proposals.parent)
    _ledger(ledger, [])
    code, original = _run_launcher(tmp_path, "2026-08", portfolio=portfolio, ledger=ledger, source_proposals=proposals)
    assert code == 0
    report_path = _report_path(tmp_path, "2026-08")
    markdown_path = _markdown_path(tmp_path, "2026-08")
    history_path = tmp_path / "monthly/monthly-source-discovery-history.json"
    before = {path: path.read_bytes() for path in (report_path, markdown_path, history_path)}

    accepted_url = SOURCE_GAP_SEED_CATALOG[0].url
    _ledger(ledger, [_ledger_entry(accepted_url, "accept")])
    code, changed = _run_launcher(tmp_path, "2026-08", portfolio=portfolio, ledger=ledger, source_proposals=proposals)
    assert code == 0
    assert changed["evidence_digest"] != original["evidence_digest"]
    assert changed["operation_identity"] != original["operation_identity"]
    after = {path: path.read_bytes() for path in (report_path, markdown_path, history_path)}
    assert any(after[path] != before[path] for path in after)
    history = json.loads(history_path.read_text(encoding="utf-8"))
    assert [entry["operation_month"] for entry in history["entries"]] == ["2026-08"]
    assert history["entries"][0]["operation_identity"] == changed["operation_identity"]

    retry_before = dict(after)
    code, retry = _run_launcher(tmp_path, "2026-08", portfolio=portfolio, ledger=ledger, source_proposals=proposals)
    assert code == 0
    assert retry == changed
    assert {path: path.read_bytes() for path in retry_before} == retry_before


def test_same_month_irrelevant_input_does_not_rewrite_publication(tmp_path: Path) -> None:
    _portfolio(tmp_path / "portfolio.json")
    proposals = tmp_path / "source-proposals.json"
    _write_json(proposals, [])
    sync_state(proposals.parent)
    code, original = _run_launcher(tmp_path, "2026-08", source_proposals=proposals)
    assert code == 0
    paths = (
        _report_path(tmp_path, "2026-08"),
        _markdown_path(tmp_path, "2026-08"),
        tmp_path / "monthly/monthly-source-discovery-history.json",
    )
    before = {path: path.read_bytes() for path in paths}

    _write_json(
        proposals,
        [
            {
                "id": "irrelevant-rejected-proposal",
                "source_url": "https://example.test/irrelevant",
                "status": "rejected",
                "proposal_date": "2026-08-01",
                "editor_note": "This field is not monthly report evidence.",
            }
        ],
    )
    sync_state(proposals.parent)
    code, retry = _run_launcher(tmp_path, "2026-08", source_proposals=proposals)
    assert code == 0
    assert retry == original
    assert {path: path.read_bytes() for path in paths} == before


def test_old_markdown_edit_isolated_but_target_retry_fails_closed(tmp_path: Path, capsys: Any) -> None:
    _portfolio(tmp_path / "portfolio.json")
    seed_state(tmp_path, source_proposals=[])
    assert _run(tmp_path, capsys, "2026-07")[0] == 0
    assert _run(tmp_path, capsys, "2026-08")[0] == 0

    markdown_path = _markdown_path(tmp_path, "2026-07")
    markdown_path.write_text(markdown_path.read_text() + "tampered\n", encoding="utf-8")
    code, later = _run(tmp_path, capsys, "2026-09")
    assert code == 0
    assert later["operation_month"] == "2026-09"

    target_markdown = _markdown_path(tmp_path, "2026-08")
    target_markdown.write_text(target_markdown.read_text() + "tampered\n", encoding="utf-8")
    history_path = tmp_path / "monthly/monthly-source-discovery-history.json"
    history = json.loads(history_path.read_text())
    history_entry = next(entry for entry in history["entries"] if entry["operation_month"] == "2026-08")
    history_entry["markdown_sha256"] = hashlib.sha256(target_markdown.read_bytes()).hexdigest()
    _write_json(history_path, history)

    code, report = _run(tmp_path, capsys, "2026-08")
    assert code == 2
    assert report["status"] == "artifact_error"
    assert "disagrees" in report["error"]


def test_onboarding_progress_requires_evidence_for_every_configured_source(tmp_path: Path, capsys: Any) -> None:
    portfolio = tmp_path / "portfolio.json"
    first_url = "https://www.configured.example/first/"
    second_url = "https://configured.example/second"
    _write_json(
        portfolio,
        {
            "sources": [
                {"name": "First", "family": "award", "url": first_url, "active": True, "language": "en"},
                {"name": "Second", "family": "award", "url": second_url, "active": True, "language": "en"},
            ]
        },
    )
    seed_state(
        tmp_path,
        source_proposals=[
            {
                "id": "first",
                "source_url": "https://WWW.configured.example/first",
                "status": "accepted",
                "proposal_date": "2026-08-01",
                "lifecycle_note": "accepted in 2026-08",
                "onboarding_request": {"url": "https://WWW.configured.example/first", "run_date": "2026-08-12"},
            }
        ],
    )
    code, report = _run(tmp_path, capsys, portfolio=portfolio)
    assert code == 0
    progress = report["onboarding_progress"]
    assert progress["verified_completed_count"] == 1
    assert progress["evidence_status"] == "insufficient_evidence"
    assert progress["verified_completed_count_semantics"] == "auditable_lower_bound"
    assert progress["remaining_to_goal"] is None
    assert progress["configured_source_count"] == 2
    assert progress["configured_without_canonical_onboarding_evidence_count"] == 1
    assert normalized_url(second_url) in progress["configured_without_canonical_onboarding_evidence_urls"]
    assert progress["accepted_proposal_signal"] == {
        "semantics": "accepted_proposal_in_window_not_verified_onboarding_completion",
        "in_window_count": 1,
        "in_window_sources": [
            {"url": normalized_url(first_url), "proposal_id": "first", "proposal_date": "2026-08-01"}
        ],
        "without_completion_count": 0,
        "without_completion_urls": [],
        "without_accepted_proposal_count": 1,
        "without_accepted_proposal_urls": [normalized_url(second_url)],
    }
    markdown = _markdown_path(tmp_path, "2026-08").read_text(encoding="utf-8")
    assert "at least 1 verified" in markdown
    assert "Goal: 12" in markdown
    assert "Remaining: unknown because canonical evidence is incomplete" in markdown
    assert "not verified onboarding completion" in markdown


@pytest.mark.parametrize("tamper", ["date", "noncanonical_url", "duplicate_url", "order"])
def test_tampered_verified_sources_fail_closed(tmp_path: Path, capsys: Any, tamper: str) -> None:
    portfolio = tmp_path / "portfolio.json"
    first_url = "https://configured.example/first"
    second_url = "https://configured.example/second"
    _write_json(
        portfolio,
        {
            "sources": [
                {"name": "First", "family": "award", "url": first_url, "active": True, "language": "en"},
                {"name": "Second", "family": "award", "url": second_url, "active": True, "language": "en"},
            ]
        },
    )
    seed_state(
        tmp_path,
        source_proposals=[
            {
                "id": "first",
                "source_url": first_url,
                "status": "accepted",
                "proposal_date": "1900-01-01",
                "onboarding_request": {"url": first_url, "run_date": "2026-07-01"},
            },
            {
                "id": "second",
                "source_url": second_url,
                "status": "accepted",
                "proposal_date": "1900-01-01",
                "onboarding_request": {"url": second_url, "run_date": "2026-08-01"},
            },
        ],
    )
    assert _run(tmp_path, capsys, portfolio=portfolio)[0] == 0

    def mutate(report: dict[str, Any]) -> None:
        sources = report["onboarding_progress"]["verified_completed_sources"]
        if tamper == "date":
            sources[0]["onboarding_run_date"] = "2025-08-31"
        elif tamper == "noncanonical_url":
            sources[0]["url"] = "https://CONFIGURED.example/first"
        elif tamper == "duplicate_url":
            sources[1]["url"] = sources[0]["url"]
        else:
            sources.reverse()

    _rewrite_report_and_history(tmp_path, mutate)
    code, result = _run(tmp_path, capsys)
    assert code == 2
    assert result["status"] == "artifact_error"


@pytest.mark.parametrize("tamper", ["duplicate_url", "outside_universe", "partition"])
def test_tampered_accepted_proposal_signal_fails_closed(tmp_path: Path, capsys: Any, tamper: str) -> None:
    portfolio = tmp_path / "portfolio.json"
    first_url = "https://configured.example/first"
    second_url = "https://configured.example/second"
    _write_json(
        portfolio,
        {
            "sources": [
                {"name": "First", "family": "award", "url": first_url, "active": True, "language": "en"},
                {"name": "Second", "family": "award", "url": second_url, "active": True, "language": "en"},
            ]
        },
    )
    seed_state(
        tmp_path,
        source_proposals=[
            {"id": "first", "source_url": first_url, "status": "accepted", "proposal_date": "2026-07-01"},
            {"id": "second", "source_url": second_url, "status": "accepted", "proposal_date": "2026-07-01"},
        ],
    )
    assert _run(tmp_path, capsys, portfolio=portfolio)[0] == 0

    def mutate(report: dict[str, Any]) -> None:
        signal = report["onboarding_progress"]["accepted_proposal_signal"]
        if tamper == "duplicate_url":
            signal["in_window_sources"][1]["url"] = signal["in_window_sources"][0]["url"]
        elif tamper == "outside_universe":
            signal["in_window_sources"][1]["url"] = "https://zzzz.example/outside"
        else:
            signal["without_completion_urls"] = []
            signal["without_completion_count"] = 0

    _rewrite_report_and_history(tmp_path, mutate)
    code, result = _run(tmp_path, capsys)
    assert code == 2
    assert result["status"] == "artifact_error"


def test_matching_accepted_proposal_requires_nonblank_id_before_publication(tmp_path: Path) -> None:
    portfolio = tmp_path / "portfolio.json"
    source_url = "https://configured.example/missing-id"
    _write_json(
        portfolio,
        {"sources": [{"name": "Missing ID", "family": "award", "url": source_url, "active": True, "language": "en"}]},
    )
    proposals = tmp_path / "source-proposals.json"
    _write_json(
        proposals,
        [{"id": " ", "source_url": source_url, "status": "accepted", "proposal_date": "2026-08-01"}],
    )
    sync_state(proposals.parent)

    code, result = _run_launcher(tmp_path, "2026-08", portfolio=portfolio, source_proposals=proposals)

    assert code == 2
    assert result["status"] == "input_error"
    assert not _report_path(tmp_path, "2026-08").exists()


def test_onboarding_progress_is_exact_when_canonical_evidence_is_complete(tmp_path: Path, capsys: Any) -> None:
    portfolio = tmp_path / "portfolio.json"
    source_url = "https://configured.example/complete"
    _write_json(
        portfolio,
        {
            "sources": [
                {"name": "Complete", "family": "award", "url": source_url, "active": True, "language": "en"},
            ]
        },
    )
    seed_state(
        tmp_path,
        source_proposals=[
            {
                "id": "complete",
                "source_url": source_url,
                "status": "accepted",
                "proposal_date": "1900-01-01",
                "lifecycle_note": "legacy prose is not evidence",
                "onboarding_request": {"url": source_url, "run_date": "2026-08-12"},
            }
        ],
    )

    code, report = _run(tmp_path, capsys, portfolio=portfolio)

    assert code == 0
    progress = report["onboarding_progress"]
    assert progress["configured_source_count"] == 1
    assert progress["configured_without_canonical_onboarding_evidence_count"] == 0
    assert progress["verified_completed_count"] == 1
    assert progress["verified_completed_count_semantics"] == "exact"
    assert progress["remaining_to_goal"] == 11
    assert progress["evidence_status"] == "complete"
    assert progress["accepted_proposal_signal"] == {
        "semantics": "accepted_proposal_in_window_not_verified_onboarding_completion",
        "in_window_count": 0,
        "in_window_sources": [],
        "without_completion_count": 0,
        "without_completion_urls": [],
        "without_accepted_proposal_count": 0,
        "without_accepted_proposal_urls": [],
    }
    markdown = _markdown_path(tmp_path, "2026-08").read_text(encoding="utf-8")
    assert "Verified completed: 1 verified" in markdown
    assert "Remaining to goal: 11" in markdown
    assert "unknown because canonical evidence" not in markdown


@pytest.mark.parametrize(
    ("proposal_date", "in_window"),
    [("not-a-date", False), ("1900-01-01", False), ("2026-08-01", True)],
)
def test_proposal_signal_and_completion_evidence_are_separate(
    tmp_path: Path, capsys: Any, proposal_date: str, in_window: bool
) -> None:
    portfolio = tmp_path / "portfolio.json"
    source_url = "https://configured.example/undated"
    _write_json(
        portfolio,
        {"sources": [{"name": "Undated", "family": "award", "url": source_url, "active": True, "language": "en"}]},
    )
    seed_state(
        tmp_path,
        source_proposals=[
            {"id": "undated", "source_url": source_url, "status": "accepted", "proposal_date": proposal_date}
        ],
    )
    code, report = _run(tmp_path, capsys, portfolio=portfolio)
    assert code == 0
    progress = report["onboarding_progress"]
    assert progress["accepted_proposal_signal"] == {
        "semantics": "accepted_proposal_in_window_not_verified_onboarding_completion",
        "in_window_count": int(in_window),
        "in_window_sources": (
            [{"url": normalized_url(source_url), "proposal_id": "undated", "proposal_date": proposal_date}]
            if in_window
            else []
        ),
        "without_completion_count": 1,
        "without_completion_urls": [normalized_url(source_url)],
        "without_accepted_proposal_count": 0,
        "without_accepted_proposal_urls": [],
    }
    markdown = _markdown_path(tmp_path, "2026-08").read_text(encoding="utf-8")
    assert "Accepted proposals without structured completion: 1" in markdown


def test_read_only_inputs_and_state_are_unchanged(tmp_path: Path, capsys: Any) -> None:
    _portfolio(tmp_path / "portfolio.json")
    proposals = tmp_path / "source-proposals.json"
    ledger = tmp_path / "ledger.json"
    sidecar = ledger.with_suffix(".md")
    _write_json(proposals, [])
    sync_state(proposals.parent)
    _ledger(ledger, [])
    sidecar.write_bytes(b"sidecar is not inspected\n")
    state_dir = tmp_path / "runs/current-weekly-pilot-russian"
    protected = [
        state_dir / "candidate-history.json",
        state_dir / "workflow-log.json",
        state_dir / "pending-returned-editor-rows.json",
        state_dir / "candidate-visibility.json",
        state_dir / "editorial-profile.json",
        state_dir / "source-mentions.json",
        state_dir / "scouting-candidates.json",
    ]
    state_dir.mkdir(parents=True)
    for path in protected:
        path.write_bytes(b"protected bytes\n")
    inputs = [tmp_path / "portfolio.json", proposals, ledger, sidecar, *protected]
    before = {path: path.read_bytes() for path in inputs}
    databases = state_databases()

    code, report = _run(tmp_path, capsys)
    assert code == 0
    assert report["next_action"]
    assert {path: path.read_bytes() for path in inputs} == before
    assert state_databases() == databases


def test_history_entry_without_pair_and_pair_without_history_fail_closed(tmp_path: Path, capsys: Any) -> None:
    _portfolio(tmp_path / "portfolio.json")
    seed_state(tmp_path, source_proposals=[])
    assert _run(tmp_path, capsys)[0] == 0
    history_path = tmp_path / "monthly/monthly-source-discovery-history.json"
    history = json.loads(history_path.read_text())
    history["entries"] = []
    _write_json(history_path, history)
    code, report = _run(tmp_path, capsys, "2026-09")
    assert code == 2
    assert report["status"] == "artifact_error"


def test_output_input_alias_is_rejected_before_creating_a_missing_ledger(tmp_path: Path, capsys: Any) -> None:
    _portfolio(tmp_path / "portfolio.json")
    seed_state(tmp_path, source_proposals=[])
    missing_ledger = tmp_path / "monthly/reports/2026-08/monthly-source-discovery.json"
    code, report = _run(tmp_path, capsys, ledger=missing_ledger)
    assert code == 2
    assert report["status"] == "input_error"
    assert not missing_ledger.exists()
    assert not (tmp_path / "monthly").exists()


def test_default_monthly_output_is_ignored_by_git() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "git",
            "check-ignore",
            "--no-index",
            "data/monthly-source-discovery/reports/2026-08/monthly-source-discovery.json",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0


def test_public_monthly_help_removes_legacy_and_family_flags() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "autoscout", "monthly-source-discovery", "--help"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    for flag in ("--history", "--history-file", "--proposals", "--signal-family", "--probe"):
        assert flag not in result.stdout
    for flag in ("--portfolio", "--review", "--ledger", "--source-proposals", "--output-dir"):
        assert flag in result.stdout


def test_monthly_reports_stay_out_of_git() -> None:
    root = Path(__file__).resolve().parents[1]
    assert "/data/monthly-source-discovery/" in (root / ".gitignore").read_text(encoding="utf-8")


def test_real_public_launcher_rejects_probe_without_artifacts(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    portfolio = tmp_path / "portfolio.json"
    proposals = tmp_path / "source-proposals.json"
    ledger = tmp_path / "missing-ledger.json"
    output = tmp_path / "monthly"
    _portfolio(portfolio)
    _write_json(proposals, [])
    sync_state(proposals.parent)
    result = subprocess.run(
        [
            str(root / "scripts/add-source"),
            "monthly",
            "--operation-month",
            "2026-08",
            "--portfolio",
            str(portfolio),
            "--source-proposals",
            str(proposals),
            "--ledger",
            str(ledger),
            "--output-dir",
            str(output),
            "--probe",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert not output.exists()


@pytest.mark.parametrize("command_kind", ["script", "module"])
@pytest.mark.parametrize("protected_root", ["configs", "runs"])
def test_public_monthly_commands_reject_protected_output_roots_without_writes(
    tmp_path: Path, command_kind: str, protected_root: str
) -> None:
    root = Path(__file__).resolve().parents[1]
    portfolio = tmp_path / "portfolio.json"
    proposals = tmp_path / "source-proposals.json"
    ledger = tmp_path / "missing-ledger.json"
    output_dir = tmp_path / protected_root
    _portfolio(portfolio)
    _write_json(proposals, [])
    sync_state(proposals.parent)
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    common_arguments = [
        "--operation-month",
        "2026-08",
        "--portfolio",
        str(portfolio),
        "--ledger",
        str(ledger),
        "--source-proposals",
        str(proposals),
        "--output-dir",
        str(output_dir),
    ]
    command = (
        [str(root / "scripts/add-source"), "monthly", *common_arguments]
        if command_kind == "script"
        else [sys.executable, "-m", "autoscout", "monthly-source-discovery", *common_arguments]
    )
    result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)

    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["status"] == "artifact_error"
    assert not output_dir.exists()
    after = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before


def test_monthly_launcher_is_the_deterministic_schedule_target(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    launcher = scripts / "add-source"
    source_launcher = Path(__file__).resolve().parents[1] / "scripts/add-source"
    launcher.write_text(source_launcher.read_text(encoding="utf-8"), encoding="utf-8")
    launcher.chmod(0o755)
    fake_python = root / ".venv/bin/python"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text("#!/usr/bin/env bash\nprintf '%s ' \"$@\"\n", encoding="utf-8")
    fake_python.chmod(0o755)

    result = subprocess.run(
        [str(launcher), "monthly", "--operation-month", "2026-08"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "-m autoscout monthly-source-discovery --operation-month 2026-08" in result.stdout
    assert "probe-source" not in result.stdout
    assert "add-source " not in result.stdout
