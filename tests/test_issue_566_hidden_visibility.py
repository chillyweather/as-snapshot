"""Issue #566 public visibility and configured-source regression seams."""

import json
from pathlib import Path

from autoscout.config import resolve_weekly_portfolio
from autoscout.replay_fixtures import build_replay_fixture_pair
from autoscout.source_feasibility import FeasibilityVerdict
from autoscout.source_feasibility import evaluate_captured_source_evidence
from autoscout.source_feasibility import evaluate_source_feasibility
from autoscout.source_page_identity import amazon_charts_entries
from autoscout.source_page_identity import amazon_charts_ranks
from autoscout.source_page_identity import audible_chart_mention_is_grounded
from autoscout.source_page_identity import audible_de_chart_mention_is_grounded
from autoscout.source_page_identity import audible_de_entry_author_names
from autoscout.source_page_identity import link_anchor_pairs
from autoscout.source_page_identity import parse_hidden_heading_texts
from autoscout.source_page_identity import parse_hidden_text
from autoscout.source_page_identity import parse_visible_page_identity
from autoscout.source_page_identity import ranking_mention_is_grounded
from autoscout.source_page_identity import server_rendered_list_entries
from autoscout.source_page_identity import validate_page_shape

ROOT = Path(__file__).resolve().parents[1]
CONFIGURED_VERDICT_BASELINE = ROOT / "tests" / "fixtures" / "issue-566-configured-source-verdicts.json"
PUBLISHNEWS_SOURCE_NAME = "PublishNews Nielsen Não Ficção Trade"
CUSPIDE_SOURCE_NAME = "Cúspide 100 más vendidos"
BOKLISTA_SOURCE_NAME = "Bokhandlerforeningen Boklista - Generell litteratur voksne"
BOERSENBLATT_SOURCE_NAME = "Börsenblatt Sachbuch Bestseller"
CPNB_SOURCE_NAME = "CPNB Top Non-Fictie"
PUBLISHNEWS_URL = "https://www.publishnews.com.br/ranking-nielsen/semanal/9"
PUBLISHNEWS_HEADING = "Lista Nielsen-PublishNews de Mais Vendidos de Não Ficção Trade de 03/08/2026 a 09/08/2026"
PUBLISHNEWS_VISIBLE_TITLE_SELECTOR = (
    ".pn-colunas-06x07 > .pn-colunas-corpo > .pn-editoria > .pn-editoria-cabeca > .pn-editoria-titulo"
)
PUBLISHNEWS_CONTRACT_METADATA = {
    "adult_nonfiction_exclusions": [
        "eu duvido você terminar este livro",
        "colorindo com aquarela",
        "livro de atividades",
    ],
    "ranking_author_selector": ".pn-ranking-livro-autor",
    "ranking_category_marker": "Não Ficção Trade",
    "ranking_evidence_url": "source_page",
    "ranking_expected_raw_row_count": 20,
    "ranking_identifier_pattern": r"(?P<identifier>97[89][0-9-]{10,20})",
    "ranking_rank_source": "document_order",
    "ranking_reporting_period_date_format": "%d/%m/%Y",
    "ranking_reporting_period_max_age_days": 14,
    "ranking_reporting_period_pattern": r"(?P<period>[0-9]{2}/[0-9]{2}/[0-9]{4} a [0-9]{2}/[0-9]{2}/[0-9]{4})",
    "ranking_row_selector": ".pn-ranking-livro",
    "ranking_title_selector": ".pn-ranking-livro-nome",
}
PUBLISHNEWS_HELPER_METADATA = {
    "version": 1,
    "source_url": PUBLISHNEWS_URL,
    "language": "pt",
    "entries": [
        {
            "title": "Bom dia, inverno",
            "author": "Tamara Klink",
            "evidence_url": PUBLISHNEWS_URL,
            "english_title": "Good Morning, Winter",
            "search_translation": "Доброе утро, зима",
            "author_transliteration_note": "Тамара Клинк",
        },
        {
            "title": "A psicologia financeira",
            "author": "Morgan Housel",
            "evidence_url": PUBLISHNEWS_URL,
            "english_title": "The Psychology of Money",
            "search_translation": "Психология денег",
            "author_transliteration_note": "Морган Хаузел",
        },
        {
            "title": "Pai, me conta a sua história?",
            "author": "Elma Van Vliet",
            "evidence_url": PUBLISHNEWS_URL,
            "english_title": "Dad, Tell Me Your Story?",
            "search_translation": "Папа, расскажи мне свою историю",
            "author_transliteration_note": "Эльма ван Влит",
        },
    ],
}


def _valid_isbn13(position: int) -> str:
    body = f"979{position:09d}"
    checksum = sum((1 if index % 2 == 0 else 3) * int(value) for index, value in enumerate(body))
    return body + str((10 - checksum % 10) % 10)


def _publishnews_replay_evidence() -> dict[str, object]:
    rows: list[str] = []
    for rank in range(1, 21):
        if rank == 1:
            title, author, identifier = "Eu duvido você terminar este livro", "Giuliana Mafra", "9780306406157"
        elif rank == 2:
            title, author, identifier = "Bom dia, inverno", "Tamara Klink", "9788535944679"
        elif rank == 3:
            title, author, identifier = "A psicologia financeira", "Morgan Housel", "9786555111101"
        elif rank == 4:
            title, author, identifier = "Pai, me conta a sua história?", "Elma Van Vliet", "9788543107790"
        else:
            title, author, identifier = (
                f"Deterministic adult nonfiction row {rank}",
                f"Deterministic fixture author {rank}",
                _valid_isbn13(rank),
            )
        rows.append(
            '<div class="pn-ranking-livro">'
            f'<div class="pn-ranking-livro-nome">{title}</div>'
            f'<div class="pn-ranking-livro-autor">{author}</div>'
            f"<span hidden>{identifier}</span></div>"
        )
    return {
        "collection_mode": "source-candidate-probe",
        "source_name": PUBLISHNEWS_SOURCE_NAME,
        "source_url": PUBLISHNEWS_URL,
        "source_page": PUBLISHNEWS_URL,
        "resolved_url": PUBLISHNEWS_URL,
        "head_status": 200,
        "get_status": 200,
        "reference_date": "2026-08-22",
        "contract_family": "ranking-table",
        "contract_metadata": PUBLISHNEWS_CONTRACT_METADATA,
        "visible_title_selector": PUBLISHNEWS_VISIBLE_TITLE_SELECTOR,
        "signal_family": "bestseller/ranking",
        "html": (
            "<!doctype html><html><body>"
            '<div class="pn-colunas-06x07"><div class="pn-colunas-corpo"><div class="pn-editoria">'
            f'<div class="pn-editoria-cabeca"><div class="pn-editoria-titulo">{PUBLISHNEWS_HEADING}</div></div>'
            "</div></div></div>" + "".join(rows) + "</body></html>"
        ),
    }


def _generic_verdict_record(verdict: FeasibilityVerdict) -> dict[str, object]:
    return {
        "outcome": verdict.outcome,
        "reason_code": verdict.reason_code,
        "detected_heading": verdict.detected_heading,
        "bounded_entry_count": verdict.bounded_entry_count,
        "samples": [{"title": sample.title, "author": sample.author, "url": sample.url} for sample in verdict.samples],
    }


def test_publishnews_baseline_uses_the_generated_replay_fixture() -> None:
    """Derive the baseline from the production replay page, not live HTML."""
    pair = build_replay_fixture_pair(
        PUBLISHNEWS_URL,
        "publishnews-nielsen-nao-ficcao-trade",
        _publishnews_replay_evidence(),
        source_name=PUBLISHNEWS_SOURCE_NAME,
        signal_family="bestseller/ranking",
        contract_family="ranking-table",
        contract_metadata=PUBLISHNEWS_CONTRACT_METADATA,
        signal_strength="top ranking",
        candidate_category="adult nonfiction",
        language="pt",
        helper_metadata=PUBLISHNEWS_HELPER_METADATA,
    )
    assert pair.entry_count == 3
    assert pair.page["reference_date"] == "2026-08-22"
    assert pair.page["html"].count('class="pn-ranking-livro"') == 20
    generated_verdict = evaluate_captured_source_evidence(
        {
            "source_url": pair.page["source_url"],
            "resolved_url": pair.page["resolved_url"],
            "head_status": 200,
            "get_status": 200,
            "html": pair.page["html"],
        }
    )
    baseline = json.loads(CONFIGURED_VERDICT_BASELINE.read_text(encoding="utf-8"))
    assert _generic_verdict_record(generated_verdict) == baseline[PUBLISHNEWS_SOURCE_NAME]


def test_parse_hidden_heading_texts_returns_only_hidden_headings() -> None:
    markup = (
        "<h1>Visible heading</h1>"
        '<div style="display:none"><h2>Hidden heading</h2><p>Hidden prose</p></div>'
        "<template><h3>Template heading</h3></template>"
    )
    assert parse_hidden_heading_texts(markup) == ["Hidden heading"]


def test_visible_page_identity_excludes_hidden_ancestor_and_hidden_only_text() -> None:
    hidden_ancestor = '<div hidden><span class="title">Hidden descendant</span></div>'
    assert parse_visible_page_identity(hidden_ancestor, ".title").failure_cause == "hidden"

    hidden_child_only = '<div class="title"><span aria-hidden="true">Only hidden child</span></div>'
    assert parse_visible_page_identity(hidden_child_only, ".title").failure_cause == "empty"


def test_link_anchor_pairs_returns_only_visible_anchor_text() -> None:
    markup = (
        '<div style="display: none !important"><a href="/hidden">Hidden link</a></div>'
        '<div><a href="/visible">Visible <span hidden>secret</span> link</a></div>'
        '<template><a href="/template">Template link</a></template>'
        '<a aria-hidden="true" href="/aria">ARIA link</a>'
    )
    assert link_anchor_pairs(markup, "https://example.test/list") == [("https://example.test/visible", "Visible link")]


def test_server_rendered_entries_exclude_hidden_links_and_context_text() -> None:
    markup = (
        '<ul><li hidden><a href="/hidden">Hidden Book</a><span class="author">Hidden Author</span></li>'
        '<li><a href="/visible">Visible Book <span style="visibility: hidden">secret title</span></a>'
        '<span class="author">Visible Author <span aria-hidden="true">fake author</span></span></li></ul>'
    )
    entries = server_rendered_list_entries(markup, "https://example.test/list")
    assert [(entry.url, entry.title) for entry in entries] == [("https://example.test/visible", "Visible Book")]
    assert "secret" not in entries[0].context.casefold()
    assert entries[0].author_elements == ("Visible Author",)


def test_amazon_chart_entries_and_ranks_ignore_hidden_blocks() -> None:
    markup = (
        '<div id="rank1"><img src="cover.jpg"><div class="kc-rank-card-title">Visible Book '
        '<span aria-hidden="true">secret title</span></div><a href="/dp/0123456789">Visible product</a></div>'
        '<div id="rank2" style="display: none!important"><div class="kc-rank-card-title">Hidden Book</div>'
        '<a href="/dp/9876543210">Hidden product</a></div>'
        '<div id="rank3"><div hidden><div class="kc-rank-card-title">Hidden nested title</div>'
        '<a href="/dp/1111111111">Hidden nested product</a></div></div>'
    )
    entries = amazon_charts_entries(markup, "https://example.test/charts")
    assert entries == [("https://example.test/dp/0123456789", "Visible Book", "Visible Book Visible product")]
    assert amazon_charts_ranks(markup, "https://example.test/charts") == [1, 3]


def test_audible_product_window_uses_visible_author_marker_only() -> None:
    markup = (
        '<li class="productlistitem"><ol><li><span class="rank">1.</span>'
        '<a href="/pd/0123456789">Visible Book</a></li>'
        '<li>By: Visible Author <span style="display: none">By: Hidden Author</span></li></ol></li>'
    )
    source_page = "https://example.test/chart"
    evidence_url = "https://example.test/pd/0123456789"
    assert audible_chart_mention_is_grounded(
        markup,
        source_page,
        rank=1,
        title="Visible Book",
        authors=["Visible Author"],
        evidence_url=evidence_url,
    )
    assert not audible_chart_mention_is_grounded(
        markup,
        source_page,
        rank=1,
        title="Visible Book",
        authors=["Hidden Author"],
        evidence_url=evidence_url,
    )


def test_audible_de_author_windows_use_visible_marker_text_only() -> None:
    markup = (
        '<li><span class="rank">1</span><a href="/pd/0123456789">Visible Book</a>'
        "<span>Von:</span><span>Visible Author</span><span>Later visible text</span>"
        "<span hidden>Von: Hidden Author</span></li>"
    )
    source_page = "https://example.test/chart"
    evidence_url = "https://example.test/pd/0123456789"
    assert audible_de_entry_author_names(markup) == {"/pd/0123456789": ["Visible Author"]}
    assert audible_de_chart_mention_is_grounded(
        markup,
        source_page,
        rank=1,
        title="Visible Book",
        authors=["Visible Author"],
        evidence_url=evidence_url,
    )
    assert not audible_de_chart_mention_is_grounded(
        markup,
        source_page,
        rank=1,
        title="Visible Book",
        authors=["Hidden Author"],
        evidence_url=evidence_url,
    )


def test_audible_de_inline_formatted_title_keeps_punctuation() -> None:
    markup = (
        '<li><span class="rank">1</span>'
        '<a href="/pd/0123456789"><h3><b>Dune</b>, Book 1</h3></a>'
        "<span>Von:</span><span>Frank Herbert</span></li>"
    )
    assert audible_de_chart_mention_is_grounded(
        markup,
        "https://example.test/chart",
        rank=1,
        title="Dune, Book 1",
        authors=["Frank Herbert"],
        evidence_url="https://example.test/pd/0123456789",
    )


def test_benign_captcha_script_is_not_hidden_text_or_a_blocked_response() -> None:
    source_url = "https://example.test/shortlist"
    html = (
        "<h1>Visible Prize Shortlist</h1>"
        '<ul><li><a href="https://example.test/shortlist/book">Visible Book</a>'
        '<span class="author">Visible Author</span></li></ul>'
        "<script>const captcha = window.widget;</script>"
    )
    verdict = evaluate_captured_source_evidence(
        {
            "source_url": source_url,
            "resolved_url": source_url,
            "head_status": 200,
            "get_status": 200,
            "html": html,
        }
    )
    assert verdict.accepted
    assert verdict.reason_code == "ok"
    assert parse_hidden_text(html) == ""


def test_captured_evidence_does_not_accept_hidden_heading() -> None:
    source_url = "https://example.test/shortlist"
    html = (
        '<h1 style="display: none">Hidden Prize Shortlist</h1>'
        '<ul><li><a href="https://example.test/shortlist/book">Visible Book</a>'
        '<span class="author">Visible Author</span></li></ul>'
    )
    verdict = evaluate_captured_source_evidence(
        {
            "source_url": source_url,
            "resolved_url": source_url,
            "head_status": 200,
            "get_status": 200,
            "html": html,
        }
    )
    assert not verdict.accepted
    assert verdict.reason_code == "missing_heading"
    assert verdict.detected_heading == ""


def test_captured_evidence_detects_a_hidden_captcha_heading() -> None:
    source_url = "https://example.test/shortlist"
    for marker in (
        "<div hidden><h1>CAPTCHA</h1></div>",
        '<div style="visibility:hidden"><h2>Captcha required</h2></div>',
        '<div aria-hidden="true"><h3>Please complete the captcha</h3></div>',
        '<h1 style="display:none">CAPTCHA</h1>',
    ):
        html = (
            f"<h1>Visible Prize Shortlist</h1>{marker}"
            '<ul><li><a href="https://example.test/shortlist/book">Visible Book</a>'
            '<span class="author">Visible Author</span></li></ul>'
        )
        verdict = evaluate_captured_source_evidence(
            {
                "source_url": source_url,
                "resolved_url": source_url,
                "head_status": 200,
                "get_status": 200,
                "html": html,
            }
        )
        assert not verdict.accepted
        assert verdict.reason_code == "blocked_response"


def test_a_hidden_captcha_widget_badge_is_not_a_blocked_response() -> None:
    """A hidden badge must behave exactly like a visible one (Issue #566 review).

    Treating a bare hidden CAPTCHA marker as a bot wall rejected collectable
    sources whose reCAPTCHA badge is hidden by an inline style, while the same
    badge left visible was accepted. Only a heading is a challenge signal.
    """
    source_url = "https://example.test/shortlist"
    entries = (
        '<ul><li><a href="https://example.test/shortlist/book">Visible Book</a>'
        '<span class="author">Visible Author</span></li></ul>'
    )
    badge = '<div class="grecaptcha-badge">protected by reCAPTCHA Privacy Terms</div>'
    hidden_badge = '<div class="grecaptcha-badge" style="visibility:hidden">protected by reCAPTCHA Privacy Terms</div>'
    for markup in (badge, hidden_badge):
        verdict = evaluate_captured_source_evidence(
            {
                "source_url": source_url,
                "resolved_url": source_url,
                "head_status": 200,
                "get_status": 200,
                "html": f"<h1>Visible Prize Shortlist</h1>{markup}{entries}",
            }
        )
        assert verdict.accepted
        assert verdict.reason_code == "ok"


def test_ranking_grounding_uses_visible_rows_and_source_statement_only() -> None:
    visible_row = "<table><tr><td>1</td><td>Visible Book</td><td>Visible Author</td><td>978-1-234</td></tr></table>"
    hidden_isbn = visible_row.replace("<td>978-1-234</td>", "<td hidden>978-1-234</td>")
    assert ranking_mention_is_grounded(
        visible_row + "Source: Circana BookScan week 2026-07-18",
        title="Visible Book",
        primary_author="Visible Author",
        isbn="978-1-234",
        current_rank="1",
        source_week="2026-07-18",
    )
    assert not ranking_mention_is_grounded(
        hidden_isbn + "Source: Circana BookScan week 2026-07-18",
        title="Visible Book",
        primary_author="Visible Author",
        isbn="978-1-234",
        current_rank="1",
        source_week="2026-07-18",
    )
    hidden_statement = visible_row + '<div style="display: none">Source: Circana BookScan week 2026-07-18</div>'
    assert not ranking_mention_is_grounded(
        hidden_statement,
        title="Visible Book",
        primary_author="Visible Author",
        isbn="978-1-234",
        current_rank="1",
        source_week="2026-07-18",
    )


def test_validate_page_shape_uses_visible_heading_and_markers() -> None:
    source = {
        "expected_page_heading": "Visible heading",
        "expected_visible_markers": ["Visible marker"],
    }
    hidden_heading = {
        "html": "<h1 hidden>Visible heading</h1><p>Visible marker</p>",
    }
    assert "expected page heading" in validate_page_shape(source, hidden_heading)
    hidden_marker = {
        "html": '<h1>Visible heading</h1><p style="visibility: hidden">Visible marker</p>',
    }
    assert "expected visible markers" in validate_page_shape(source, hidden_marker)


def test_configured_source_fixture_verdicts_match_head_baseline() -> None:
    baseline = json.loads(CONFIGURED_VERDICT_BASELINE.read_text(encoding="utf-8"))
    pilot_path = ROOT / "configs" / "current-weekly-pilot.json"
    pilot = resolve_weekly_portfolio(json.loads(pilot_path.read_text(encoding="utf-8")), pilot_path)
    fixture_sources = [source for source in pilot["sources"] if source.get("source_page_fixture_file")]
    non_fixture_sources = [source for source in pilot["sources"] if not source.get("source_page_fixture_file")]
    # #945, #955: the PM deals and PW issue sources replay a recorded pass result, not a captured page.
    assert [source["name"] for source in non_fixture_sources] == [
        "Nonfiction.fr Critiques",
        "Publishers Marketplace Deals",
        "Publishers Weekly Nonfiction Reviews",
    ]
    assert len(pilot["sources"]) == len(fixture_sources) + len(non_fixture_sources)
    actual: dict[str, object] = {}
    for source in fixture_sources:
        fixture_name = source.get("source_page_fixture_file")
        if not fixture_name:
            continue
        page = json.loads((ROOT / "configs" / fixture_name).resolve().read_text(encoding="utf-8"))
        source_url = page.get("source_page") or page.get("source_url") or source["url"]
        evidence = {
            "source_url": source_url,
            "resolved_url": page.get("resolved_url", source_url),
            "head_status": 200,
            "get_status": 200,
            "html": page.get("html", ""),
        }
        if source["name"] == BOKLISTA_SOURCE_NAME:
            # Boklista was pinned after configured ranking dispatch existed.
            # Older entries retain the historical generic captured-source baseline.
            evidence["source_page"] = page.get("source_page", source_url)
            evidence["identity_html"] = page.get("identity_html", "")
            evidence["reference_date"] = page.get("reference_date")
            verdict = evaluate_source_feasibility(source, evidence)
        else:
            verdict = evaluate_captured_source_evidence(evidence)
        actual[source["name"]] = {
            "outcome": verdict.outcome,
            "reason_code": verdict.reason_code,
            "detected_heading": verdict.detected_heading,
            "bounded_entry_count": verdict.bounded_entry_count,
            "samples": [
                {"title": sample.title, "author": sample.author, "url": sample.url} for sample in verdict.samples
            ],
        }
    actual_names = set(actual)
    baseline_names = set(baseline)
    assert actual_names <= baseline_names
    unconfigured_baseline_names = baseline_names - actual_names
    allowed_unconfigured_names = {
        source_name
        for source_name in (
            PUBLISHNEWS_SOURCE_NAME,
            CUSPIDE_SOURCE_NAME,
            BOKLISTA_SOURCE_NAME,
            BOERSENBLATT_SOURCE_NAME,
            CPNB_SOURCE_NAME,
            "BookInfo Weekly Nonfiction",
            "Independent Press Top 40 Nonfiction",
            "Australian Book Industry Awards (ABIA) 2026 — Nonfiction",
            "Nagroda Złota Róża 2026 — nominacje",
            "Perlentaucher Bücherschau des Tages",
            "Kirkus Reviews Coming Soon Nonfiction",
        )
        if source_name not in actual_names
    }
    assert unconfigured_baseline_names <= allowed_unconfigured_names
    if PUBLISHNEWS_SOURCE_NAME in actual_names:
        assert PUBLISHNEWS_SOURCE_NAME in baseline_names

    changed = [name for name in actual if baseline[name] != actual[name]]
    assert changed == []
    expected_baseline = {name: actual[name] for name in baseline if name in actual_names}
    expected_baseline.update({name: baseline[name] for name in baseline if name not in actual_names})
    assert json.dumps(expected_baseline, ensure_ascii=False, indent=2) + "\n" == CONFIGURED_VERDICT_BASELINE.read_text(
        encoding="utf-8"
    )
