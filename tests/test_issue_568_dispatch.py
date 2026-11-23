"""Pure contract-family dispatch regression tests for issue #568.

Configured-source verdicts remain pinned by the Issue #566 fixture.
PublishNews and Boklista use the declared ranking-table contract when onboarding
is staged; their pre-onboarding absence is allowed without weakening the existing
sources.
Audible chart evaluators now use declared author-marker metadata, and Amazon
chart evaluators now enforce landing-page evidence instead of generic linked-list parsing.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from autoscout.config import resolve_weekly_portfolio
from autoscout.source_feasibility import evaluate_source_feasibility
from autoscout.source_probe import SourceCandidateLedgerError
from autoscout.source_probe import run_source_candidate_probe

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests/fixtures/issue-566-configured-source-verdicts.json"

PUBLISHNEWS_SOURCE_NAME = "PublishNews Nielsen Não Ficção Trade"
CUSPIDE_SOURCE_NAME = "Cúspide 100 más vendidos"
BOKLISTA_SOURCE_NAME = "Bokhandlerforeningen Boklista - Generell litteratur voksne"

EXPECTED_DISPATCH_CHANGES = {
    "Verso — Forthcoming": ("accept", "ok", 3),
    "Beacon Press — Forthcoming": ("accept", "ok", 3),
    # #921: official Brazilian biography/reportage award rows.
    "Prêmio Jabuti — Não Ficção": ("accept", "ok", 10),
    # #901: confirmed Spanish dated frontlist cards use their explicit contract.
    "NuevosLibros — Ensayo": ("accept", "ok", 3),
    # #902: confirmed Spanish dated frontlist cards use their explicit contract.
    "NuevosLibros — Historia": ("accept", "ok", 3),
    # #906: confirmed Spanish dated frontlist cards use their explicit contract.
    "NuevosLibros — Política y sociedad": ("accept", "ok", 3),
    # #908: confirmed Spanish dated frontlist cards use their explicit contract.
    "NuevosLibros — Divulgación": ("accept", "ok", 3),
    # #907: confirmed Spanish dated frontlist cards use their explicit contract.
    "NuevosLibros — Biografía y memorias": ("accept", "ok", 3),
    # The explicit Audible chart evaluator accepts the 20 English rows.
    "Audible Bestselling Science & Engineering Audiobooks": ("accept", "ok", 0),
    "Audible Bestselling Business & Careers Audiobooks": ("accept", "ok", 0),
    "Audible Bestselling Computers & Technology Audiobooks": ("accept", "ok", 0),
    # The configured German marker is ``von`` and the real Audible.de
    # evaluator accepts its 20-row fixture without URL inference.
    "Audible.de Bestselling Biografien & Erinnerungen Hoerbuecher": ("accept", "ok", 0),
    # Amazon chart fixtures contain no retained landing-page evidence.
    "Amazon Charts Most Read Nonfiction": ("reject", "landing_evidence", 0),
    "Amazon.de Charts Meistgelesene Sachbücher": ("reject", "landing_evidence", 0),
    # The generated replay page contains 20 safe ranking rows, so its
    # contract evaluation accepts all 20 rows.
    PUBLISHNEWS_SOURCE_NAME: ("accept", "ok", 20),
    # The generated Cúspide replay page supplies the retained detail pages
    # required by its bounded detail route.
    CUSPIDE_SOURCE_NAME: ("accept", "ok", 16),
    # #775: explicit date selector and retained ISBN author proof accept all 25 rows.
    "Börsenblatt Sachbuch Bestseller": ("accept", "ok", 25),
    # #776: editor authorized CPNB; its declared ranking contract retains 20 rows.
    "CPNB Top Non-Fictie": ("accept", "ok", 20),
    # #777: editor approved bounded JSON and three explicit exclusions.
    "BookInfo Weekly Nonfiction": ("accept", "ok", 17),
    # #780: operator approved the bounded publisher CSV and one explicit exclusion.
    "Independent Press Top 40 Nonfiction": ("accept", "ok", 39),
    # #578: operator approved ABIA; its award-list contract reads six unlinked rows in each of two categories.
    "Australian Book Industry Awards (ABIA) 2026 — Nonfiction": ("accept", "ok", 12),
    # #795: operator approved Złota Róża; its award-list contract reads four unlinked rows under a bold paragraph label.
    "Nagroda Złota Róża 2026 — nominacje": ("accept", "ok", 4),
    # #809: editor approved the Perlentaucher digest; 31 of 69 captured books carry no translator note or genre label.
    "Perlentaucher Bücherschau des Tages": ("accept", "ok", 31),
    # #816: editor approved 27 grounded nonfiction rows from the bounded pre-collection pass.
    "Kirkus Reviews Coming Soon Nonfiction": ("accept", "ok", 27),
}


def test_dispatch_preserves_pinned_configured_source_baseline() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    config_path = ROOT / "configs/current-weekly-pilot.json"
    config = resolve_weekly_portfolio(json.loads(config_path.read_text(encoding="utf-8")), config_path)
    fixture_sources = [source for source in config["sources"] if source.get("source_page_fixture_file")]

    actual: dict[str, tuple[str | None, str, int]] = {}
    for source in fixture_sources:
        page_path = (ROOT / "configs" / source["source_page_fixture_file"]).resolve()
        page = json.loads(page_path.read_text(encoding="utf-8"))
        url = page.get("source_page") or page.get("source_url") or source["url"]
        evidence: dict[str, object] = {
            "source_url": url,
            "source_page": page.get("source_page", url),
            "resolved_url": page.get("resolved_url", url),
            "head_status": 200,
            "get_status": 200,
            "html": page.get("html", ""),
        }
        if page.get("reference_date") is not None:
            evidence["reference_date"] = page["reference_date"]
        if "bookinfo_evidence" in page:
            evidence["bookinfo_evidence"] = page["bookinfo_evidence"]
        if "ipc_top40_evidence" in page:
            evidence["ipc_top40_evidence"] = page["ipc_top40_evidence"]
        if "perlentaucher_evidence" in page:
            evidence["perlentaucher_evidence"] = page["perlentaucher_evidence"]
        if "kirkus_evidence" in page:
            evidence["kirkus_evidence"] = page["kirkus_evidence"]
        if "detail_pages" in page:
            evidence["detail_pages"] = page["detail_pages"]
        if "identity_html" in page:
            evidence["identity_html"] = page["identity_html"]
            if source["name"] == BOKLISTA_SOURCE_NAME:
                assert isinstance(page["identity_html"], str) and page["identity_html"].strip()
        verdict = evaluate_source_feasibility(source, evidence)
        actual[source["name"]] = (verdict.outcome, verdict.reason_code, verdict.bounded_entry_count)

    assert set(actual) <= set(baseline)
    expected_configured_changes = set(EXPECTED_DISPATCH_CHANGES) & set(actual)
    changed = {
        name
        for name, value in actual.items()
        if value
        != (
            baseline[name]["outcome"],
            baseline[name]["reason_code"],
            baseline[name]["bounded_entry_count"],
        )
    }
    assert changed == expected_configured_changes
    for name in expected_configured_changes:
        assert actual[name] == EXPECTED_DISPATCH_CHANGES[name]


def test_probe_dispatch_persists_contract_evidence_and_never_reuses_another_family(
    tmp_path: Path,
) -> None:
    url = "https://example.test/ranking"
    period = date.today().strftime("%d/%m/%Y")
    heading = f"Synthetic Category {period} a {period}"
    valid_identifiers = ("9780306406157", "9780140328721", "9781401971366")
    rows = "".join(
        f'<div class="row"><span class="rank">{index}</span><span class="title">Book {index}</span>'
        f'<span class="author">Author {index}</span><span hidden>ISBN {valid_identifiers[(index - 1) % len(valid_identifiers)]}</span></div>'
        for index in range(1, 21)
    )
    html = f'<h1 class="title">{heading}</h1>{rows}'
    ranking_metadata: dict[str, Any] = {
        "ranking_row_selector": ".row",
        "ranking_rank_selector": ".rank",
        "ranking_title_selector": ".title",
        "ranking_author_selector": ".author",
        "ranking_identifier_pattern": r"ISBN (?P<identifier>97[89]\d{10})",
        "ranking_category_marker": "Synthetic Category",
        "ranking_reporting_period_pattern": r"(?P<period>\d{2}/\d{2}/\d{4} a \d{2}/\d{2}/\d{4})",
        "ranking_reporting_period_date_format": "%d/%m/%Y",
        "ranking_reporting_period_max_age_days": 2,
        "ranking_expected_raw_row_count": 20,
        "ranking_evidence_url": "source_page",
        "adult_nonfiction_exclusions": ["colorindo com aquarela"],
    }

    class Response:
        status = 200

        def read(self, _limit: int) -> bytes:
            return html.encode()

        def geturl(self) -> str:
            return url

        def close(self) -> None:
            return None

    calls: list[str] = []

    def opener(request: Any, **_kwargs: Any) -> Response:
        calls.append(request.get_method())
        return Response()

    report, code = run_source_candidate_probe(
        [url],
        contract_families=["ranking-table"],
        contract_metadata=[ranking_metadata],
        keep_captures=tmp_path / "captures",
        ledger_path=tmp_path / "ledger.json",
        feasibility_dir=tmp_path / "drafts",
        opener=opener,
    )
    assert code == 0
    assert report["candidates"][0]["outcome"] == "accept"
    assert report["candidates"][0]["raw_row_count"] == 20
    assert calls == ["HEAD", "GET"]
    assert list((tmp_path / "captures").glob("*.json"))
    assert (tmp_path / "ledger.json").exists()
    assert (tmp_path / "ledger.md").exists()
    assert list((tmp_path / "drafts").glob("*.md"))

    with pytest.raises(SourceCandidateLedgerError, match="ranking-table.*generic"):
        run_source_candidate_probe(
            [url],
            contract_families=["generic"],
            ledger_path=tmp_path / "ledger.json",
            feasibility_dir=tmp_path / "drafts",
            opener=lambda *_args, **_kwargs: pytest.fail("prior verdict must not trigger a request"),
        )
