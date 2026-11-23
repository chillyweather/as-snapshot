"""Issue #606 - retained POST ranking evidence reaches the public preview seam."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from boklista_test_support import boklista_post_ranking_contract
from test_issue_339_source_onboarding import _base_repo
from test_issue_339_source_onboarding import _init_git
from test_issue_339_source_onboarding import _write_json

from autoscout.__main__ import main
from autoscout.ranking_response import unwrap_ranking_response
from autoscout.source_exit_codes import EXIT_CONFIRMATION
from autoscout.source_exit_codes import EXIT_REJECT
from autoscout.source_feasibility import evaluate_source_feasibility
from autoscout.source_onboarding import OnboardingRunners
from autoscout.source_onboarding_transaction import default_replay_runner
from autoscout.source_page_identity import parse_visible_page_identity
from autoscout.source_page_identity import ranking_mention_is_grounded
from autoscout.source_page_identity import ranking_rows

ROOT = Path(__file__).resolve().parents[1]
URL = "https://example.test/wp-admin/admin-ajax.php"
NAME = "Issue 606 Boklista Ranking"
HEADING = "2026-33-Generell litteratur voksne"
CONTRACT_METADATA: dict[str, Any] = dict(boklista_post_ranking_contract(URL, expected_raw_row_count=1))


def _capture(*, body_status: int = 200) -> dict[str, Any]:
    html = (
        '<div class="book-row-cover"><div class="book-row">'
        '<div class="col1"><h3 class="book-title">Middag under 50-lappen</h3>'
        '<h3 class="author-name">Storm, Sandra</h3></div>'
        '<div class="col2"><div class="isbn">9780200000017</div></div>'
        "</div></div>"
    )
    return {
        "collection_mode": "source-candidate-probe",
        "source_url": URL,
        "source_page": URL,
        "source_name": NAME,
        "name": NAME,
        "signal_family": "bestseller/ranking",
        "contract_family": "ranking-table",
        "resolved_url": URL,
        "head_status": None,
        "get_status": body_status,
        "reference_date": "2026-08-24",
        "html": html,
        "identity_html": f'<html><body><h1 class="page-title">{HEADING}</h1></body></html>',
        "detected_heading": HEADING,
        "bounded_entry_count": 1,
        "raw_row_count": 1,
        "contract_metadata": CONTRACT_METADATA,
    }


def _write_inputs(tmp_path: Path, *, body_status: int = 200) -> dict[str, Path]:
    paths = _base_repo(tmp_path)
    _write_json(paths["evidence"], _capture(body_status=body_status))
    _write_json(
        paths["helper"],
        {
            "version": 1,
            "source_url": URL,
            "language": "no",
            "entries": [
                {
                    "title": "Middag under 50-lappen",
                    "author": "Storm, Sandra",
                    "evidence_url": URL,
                    "english_title": "Dinner Under 50 Kroner",
                    "search_translation": "ужин до 50 крон",
                    "author_transliteration_note": "Sandra Storm - Сандра Сторм",
                }
            ],
        },
    )
    paths["feasibility"].write_text(
        f"# Issue #606 feasibility evidence\n\nSource: {NAME}\n\nURL: {URL}\n\n"
        "The retained POST ranking evidence is accepted for replay-first probation onboarding.\n",
        encoding="utf-8",
    )
    return paths


def _args(paths: dict[str, Path]) -> list[str]:
    return [
        "add-source",
        "--url",
        URL,
        "--probe-evidence",
        str(paths["evidence"]),
        "--helper-metadata",
        str(paths["helper"]),
        "--feasibility-evidence",
        str(paths["feasibility"]),
        "--feasibility-doc",
        str(paths["repo"] / "docs" / "issue-606-boklista-source-feasibility.md"),
        "--onboarding-issue",
        "606",
        "--run-date",
        "2026-08-25",
        "--reason",
        "The accepted retained POST ranking is a bounded adult nonfiction signal.",
        "--source-name",
        NAME,
        "--family",
        "bestseller/ranking",
        "--signal-family",
        "bestseller/ranking",
        "--access-status",
        "open",
        "--check-frequency",
        "weekly",
        "--language",
        "no",
        "--country",
        "Norway",
        "--market",
        "Norway",
        "--expected-category",
        "Generell litteratur voksne",
        "--refresh-cadence",
        "weekly",
        "--url-stability",
        "stable weekly POST endpoint",
        "--extraction-notes",
        "Metadata-only ranked titles and primary authors.",
        "--expected-page-heading",
        HEADING,
        "--expected-signal-strength",
        "top ranking",
        "--eligible-candidate-category",
        "adult nonfiction",
        "--lifecycle-note",
        "Issue #606 active and reversible during probation.",
        "--notes",
        "Issue #606 accepted Source Proposal; replay-first onboarding.",
        "--contract-family",
        "ranking-table",
        "--metadata-ranking-table",
        "--ranking-row-selector",
        CONTRACT_METADATA["ranking_row_selector"],
        "--visible-title-selector",
        CONTRACT_METADATA["visible_title_selector"],
        "--ranking-rank-source",
        CONTRACT_METADATA["ranking_rank_source"],
        "--ranking-title-selector",
        CONTRACT_METADATA["ranking_title_selector"],
        "--ranking-author-selector",
        CONTRACT_METADATA["ranking_author_selector"],
        "--ranking-request-method",
        CONTRACT_METADATA["ranking_request_method"],
        "--ranking-request-page-field",
        CONTRACT_METADATA["ranking_request_page_field"],
        "--ranking-request-max-pages",
        str(CONTRACT_METADATA["ranking_request_max_pages"]),
        "--ranking-response-last-page-field",
        CONTRACT_METADATA["ranking_response_last_page_field"],
        "--ranking-identity-url-template",
        CONTRACT_METADATA["ranking_identity_url_template"],
        "--ranking-identity-fields",
        CONTRACT_METADATA["ranking_identity_fields"][0],
        "--ranking-identity-fields",
        CONTRACT_METADATA["ranking_identity_fields"][1],
        "--ranking-response-html-field",
        CONTRACT_METADATA["ranking_response_html_field"],
        "--ranking-request-body",
        CONTRACT_METADATA["ranking_request_body"][0],
        "--ranking-request-body",
        CONTRACT_METADATA["ranking_request_body"][1],
        "--ranking-request-body",
        CONTRACT_METADATA["ranking_request_body"][2],
        "--ranking-request-body",
        CONTRACT_METADATA["ranking_request_body"][3],
        "--ranking-identifier-selector",
        CONTRACT_METADATA["ranking_identifier_selector"],
        "--ranking-category-marker",
        CONTRACT_METADATA["ranking_category_marker"],
        "--ranking-reporting-period-undated",
        "--ranking-expected-raw-row-count",
        str(CONTRACT_METADATA["ranking_expected_raw_row_count"]),
        "--ranking-evidence-url",
        CONTRACT_METADATA["ranking_evidence_url"],
        "--ranking-exclusion-term",
        CONTRACT_METADATA["adult_nonfiction_exclusions"][0],
        "--proposals-file",
        str(paths["proposal"]),
        "--portfolio-config",
        str(paths["portfolio"]),
        "--pilot-config",
        str(paths["pilot"]),
        "--pilot-live-config",
        str(paths["pilot_live"]),
        "--live-config",
        str(paths["live"]),
        "--repo-root",
        str(paths["repo"]),
    ]


def test_post_retained_capture_reaches_public_preview_confirmation(capsys: Any, tmp_path: Path) -> None:
    paths = _write_inputs(tmp_path)

    assert main(_args(paths)) == EXIT_CONFIRMATION
    report = json.loads(capsys.readouterr().out)

    assert report["confirmation_required"] is True
    assert report["outcome"] == "confirmation_required"
    assert report["stage"] == "proposal"
    assert next(stage for stage in report["stages"] if stage["stage"] == "probe")["outcome"] == "accept"
    assert report["representative_entries"] == [
        {
            "title": "Middag under 50-lappen",
            "author": "Storm, Sandra",
            "evidence_url": URL,
        }
    ]


def test_confirmed_post_retained_capture_validates_replay_with_post_policy(
    capsys: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _write_inputs(tmp_path)
    _init_git(paths["repo"])

    assert main(_args(paths)) == EXIT_CONFIRMATION
    preview = json.loads(capsys.readouterr().out)
    runners = OnboardingRunners(
        replay=default_replay_runner,
        contracts=lambda *_args, **_kwargs: {"status": "passed"},
        full_suite=lambda *_args, **_kwargs: {"status": "passed"},
    )
    monkeypatch.setattr("autoscout.source_onboarding_transaction.default_onboarding_runners", lambda: runners)

    assert main([*_args(paths), "--confirmation-token", preview["confirmation_token"], "--confirmed-by-editor"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["outcome"] == "completed"
    replay_stage = next(stage for stage in report["stages"] if stage["stage"] == "replay")
    assert replay_stage["source_run"]["status"] == "collected"
    assert replay_stage["source_run"]["raw_mention_count"] == 1
    assert replay_stage["source_run"]["valid_mention_count"] == 1
    assert replay_stage["source_run"]["skipped_item_count"] == 0

    published_dir = paths["repo"] / "data" / "current-weekly-pilot"
    page_path = next(published_dir.glob("codex-capture-*.json"))
    extraction_path = next(published_dir.glob("*-codex-extraction.json"))
    page = json.loads(page_path.read_text(encoding="utf-8"))
    extraction = json.loads(extraction_path.read_text(encoding="utf-8"))
    rows = ranking_rows(page["html"], CONTRACT_METADATA)
    assert [(row.rank, row.title, row.author, row.identifier) for row in rows] == [
        ("1", "Middag under 50-lappen", "Storm, Sandra", "9780200000017")
    ]
    response = unwrap_ranking_response(json.dumps(page["ranking_response_envelope"]), CONTRACT_METADATA)
    assert response.ok is True
    assert response.html == page["html"]
    assert response.last_page_value(CONTRACT_METADATA) is True
    assert set(page["ranking_response_envelope"]) == {"html", "last"}
    assert page["ranking_response_envelope"]["html"] == page["html"]
    assert page["contract_metadata"]["adult_nonfiction_exclusions"] == ["barn"]
    assert page["contract_metadata"]["visible_title_selector"] == ".page-title"
    assert page["contract_metadata"]["lifecycle_status"] == "probation"

    identity_html = page["identity_html"]
    assert '<h1 class="page-title">' in identity_html
    assert HEADING in identity_html
    identity = parse_visible_page_identity(identity_html, ".page-title")
    assert identity.failure_cause is None
    assert identity.identity_texts == (HEADING,)
    assert len(identity_html.encode("utf-8")) < 1_000
    for forbidden in ("script", "gtm", "wp-content"):
        assert forbidden not in identity_html.casefold()

    mention = extraction["mentions"][0]
    assert ranking_mention_is_grounded(
        page["html"],
        title=str(mention["title"]),
        isbn=str(mention["isbn"]),
        current_rank=str(mention["current_rank"]),
        source_week=str(mention["source_week"]),
        primary_authors=[str(author) for author in mention["primary_authors"]],
        ranking_source=CONTRACT_METADATA,
        evidence_url=str(mention["evidence_url"]),
        candidate_category=str(mention["candidate_category"]),
        identity_html=str(page.get("identity_html", "")),
    )


def test_tracked_boklista_fixture_preserves_contract_and_multiple_primary_authors() -> None:
    page = json.loads(
        (
            ROOT
            / "data/current-weekly-pilot/codex-capture-bokhandlerforeningen-boklista-generell-litteratur-voksne-4fc8ef89.json"
        ).read_text(encoding="utf-8")
    )
    response = unwrap_ranking_response(json.dumps(page["ranking_response_envelope"]), page["contract_metadata"])
    assert response.ok is True
    assert response.html == page["html"]
    assert response.last_page_value(page["contract_metadata"]) is True
    assert page["contract_metadata"]["adult_nonfiction_exclusions"] == ["barn"]
    assert page["contract_metadata"]["lifecycle_status"] == "probation"
    assert page["contract_metadata"]["visible_title_selector"] == ".page-title"
    identity_html = page["identity_html"]
    assert '<h1 class="page-title">' in identity_html
    assert "2026-33-Generell litteratur voksne" in identity_html
    identity = parse_visible_page_identity(identity_html, ".page-title")
    assert identity.failure_cause is None
    assert identity.identity_texts == ("2026-33-Generell litteratur voksne",)
    assert len(identity_html.encode("utf-8")) < 1_000
    for forbidden in ("script", "gtm", "wp-content"):
        assert forbidden not in identity_html.casefold()

    verdict = evaluate_source_feasibility(
        page["contract_metadata"],
        {
            "source_url": page["source_url"],
            "source_page": page["source_page"],
            "resolved_url": page["resolved_url"],
            "head_status": None,
            "get_status": 200,
            "html": page["html"],
            "identity_html": page["identity_html"],
            "reference_date": page["reference_date"],
        },
    )
    assert (verdict.outcome, verdict.reason_code, verdict.bounded_entry_count) == ("accept", "ok", 26)
    rows = ranking_rows(page["html"], page["contract_metadata"])
    assert len(rows) == 26
    assert (rows[0].title, rows[0].author) == ("Middag under 50-lappen", "Storm, Sandra")
    assert (rows[1].title, rows[1].author) == ("Vårt rike land", "Øystein Olsen / Eivind Thomassen")

    extraction = json.loads(
        (
            ROOT
            / "data/current-weekly-pilot/bokhandlerforeningen-boklista-generell-litteratur-voksne-4fc8ef89-codex-extraction.json"
        ).read_text(encoding="utf-8")
    )
    helper = json.loads(
        (
            ROOT
            / "data/current-weekly-pilot/bokhandlerforeningen-boklista-generell-litteratur-voksne-helper-metadata.json"
        ).read_text(encoding="utf-8")
    )
    mention = next(item for item in extraction["mentions"] if item["title"] == "Vårt rike land")
    assert mention["primary_authors"] == ["Øystein Olsen", "Eivind Thomassen"]
    helper_entry = next(item for item in helper["entries"] if item["title"] == "Vårt rike land")
    assert helper_entry["author"] == "Øystein Olsen / Eivind Thomassen"


def test_tracked_boklista_rejects_a_non_resolving_declared_identity_selector() -> None:
    page = json.loads(
        (
            ROOT
            / "data/current-weekly-pilot/codex-capture-bokhandlerforeningen-boklista-generell-litteratur-voksne-4fc8ef89.json"
        ).read_text(encoding="utf-8")
    )
    evidence = {
        "source_url": page["source_url"],
        "source_page": page["source_page"],
        "resolved_url": page["resolved_url"],
        "head_status": None,
        "get_status": 200,
        "html": page["html"],
        "identity_html": page["identity_html"],
        "reference_date": page["reference_date"],
    }

    baseline = evaluate_source_feasibility(page["contract_metadata"], evidence)
    assert (baseline.outcome, baseline.reason_code) == ("accept", "ok")

    contract = dict(page["contract_metadata"])
    assert contract["visible_title_selector"] == ".page-title"
    contract["visible_title_selector"] = ".missing-page-title"

    verdict = evaluate_source_feasibility(contract, evidence)
    assert verdict.outcome == "reject"
    assert verdict.reason_code == "ranking_page_identity"
    assert verdict.reason == "ranking page does not expose the declared visible category and reporting period"


def test_non_200_post_retained_capture_is_rejected_at_public_preview(capsys: Any, tmp_path: Path) -> None:
    paths = _write_inputs(tmp_path, body_status=503)

    assert main(_args(paths)) == EXIT_REJECT
    report = json.loads(capsys.readouterr().out)

    assert report["stage"] == "probe"
    assert report["reason_code"] == "http_error"
    assert report["reason"] == "POST must return 200, got 503"
