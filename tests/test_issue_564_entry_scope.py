import json
from pathlib import Path
from typing import Any

import pytest

from autoscout.__main__ import main
from autoscout.config import load_validated_config
from autoscout.config import validate_config
from autoscout.source_collectors import extraction_contract
from autoscout.source_collectors import extraction_contract_violation
from autoscout.source_feasibility import evaluate_captured_source_evidence
from autoscout.source_feasibility import evidence_from_mapping
from autoscout.source_page_identity import ranking_mention_is_grounded
from autoscout.source_page_identity import server_rendered_list_entries
from autoscout.source_probe import run_source_candidate_probe

SOURCE_URL = "https://example.test/ranking"
ROOT = Path(__file__).resolve().parents[1]


def _evidence(html: str, **options: str) -> dict[str, object]:
    return {
        "source_url": SOURCE_URL,
        "resolved_url": SOURCE_URL,
        "head_status": 200,
        "get_status": 200,
        "html": html,
        **options,
    }


def test_declared_scope_supports_deeper_direct_child_paths() -> None:
    html = (
        '<div class="outer"><section class="ranking"><div class="ranking-row">'
        '<a href="/books/one">Book One</a><span class="author">Jane Author</span>'
        "</div></section></div>"
    )

    entries = server_rendered_list_entries(html, SOURCE_URL, entry_scope_selector=".outer > .ranking > .ranking-row")
    assert [(entry.title, entry.block_tag) for entry in entries] == [("Book One", "div")]


def test_declared_div_row_keeps_nested_div_fields_in_author_context_and_grounding() -> None:
    html = (
        '<div class="ranking"><div class="ranking-row">'
        '<div><span>1</span><a href="/books/one">Book One</a></div>'
        '<div class="author">Jane Author</div><div>ISBN 978-1-234</div>'
        "</div></div>Source: Nielsen BookScan week 2026-08-17"
    )

    entries = server_rendered_list_entries(html, SOURCE_URL, entry_scope_selector=".ranking > .ranking-row")
    assert len(entries) == 1
    assert "Jane Author" in entries[0].context
    assert "ISBN 978-1-234" in entries[0].context
    assert entries[0].author_elements == ("Jane Author",)
    assert ranking_mention_is_grounded(
        html,
        title="Book One",
        primary_author="Jane Author",
        isbn="978-1-234",
        current_rank="1",
        source_week="2026-08-17",
        entry_scope_selector=".ranking > .ranking-row",
        ranking_source_statement="Source: Nielsen BookScan",
    )


def test_declared_scope_ignores_landmark_free_chrome_and_accepts_div_entry() -> None:
    html = (
        "<h1>Weekly ranking</h1>"
        '<div class="site-menu"><ul><li><a href="/newsletters">Newsletters</a></li></ul></div>'
        '<div class="ranking"><div class="ranking-row">'
        '<span>1</span><a href="/books/one">Book One</a>'
        '<span class="author">Jane Author</span></div></div>'
    )

    entries = server_rendered_list_entries(html, SOURCE_URL, entry_scope_selector=".ranking > .ranking-row")
    assert [(entry.title, entry.block_tag) for entry in entries] == [("Book One", "div")]

    verdict = evaluate_captured_source_evidence(_evidence(html, entry_scope_selector=".ranking > .ranking-row"))
    assert verdict.accepted
    assert verdict.reason_code == "ok"
    assert verdict.bounded_entry_count == 1


def test_declared_div_entry_bounds_ranking_fields_and_source_statement() -> None:
    html = (
        '<div class="ranking"><div class="ranking-row">'
        '<span>1</span><a href="/books/one">Book One</a>'
        '<span class="author">Jane Author</span><span>ISBN 978-1-234</span>'
        "</div></div>"
        "Source: Nielsen BookScan week 2026-08-17"
    )

    assert ranking_mention_is_grounded(
        html,
        title="Book One",
        primary_author="Jane Author",
        isbn="978-1-234",
        current_rank="1",
        source_week="2026-08-17",
        entry_scope_selector=".ranking > .ranking-row",
        ranking_source_statement="Source: Nielsen BookScan",
    )
    assert not ranking_mention_is_grounded(
        html,
        title="Book One",
        primary_author="Jane Author",
        isbn="978-1-234",
        current_rank="1",
        source_week="2026-08-17",
        entry_scope_selector=".ranking > .ranking-row",
        ranking_source_statement="Source: Other Provider",
    )


@pytest.mark.parametrize(
    ("html", "reason_code", "reason"),
    (
        (
            '<div class="ranking"><div class="ranking-row">One</div></div>'
            '<div class="ranking"><div class="ranking-row">Two</div></div>',
            "entry_scope_ambiguous",
            "entry scope selector matched more than one scope",
        ),
        (
            '<div class="other"><div class="ranking-row">One</div></div>',
            "entry_scope_missing",
            "entry scope selector matched no scope",
        ),
        (
            '<div class="ranking" hidden><div class="ranking-row">Hidden</div></div>',
            "entry_scope_hidden",
            "entry scope selector matched a hidden scope",
        ),
        (
            '<nav><div class="ranking"><div class="ranking-row">Chrome</div></div></nav>',
            "entry_scope_chrome",
            "entry scope selector matched a scope inside page-level chrome",
        ),
        (
            '<div class="ranking"><img class="ranking-row"></div>',
            "entry_scope_empty",
            "entry scope selector matched a scope with no visible text",
        ),
        (
            '<div class="ranking">Visible scope text<img class="ranking-row"></div>',
            "entry_scope_no_candidate_entries",
            "entry scope selector matched no visible candidate entry containers",
        ),
        (
            '<div class="ranking">Visible scope text<div class="ranking-row"></div></div>',
            "entry_scope_no_candidate_entries",
            "entry scope selector matched no visible candidate entry containers",
        ),
        (
            '<div class="ranking">Visible scope text<div class="ranking-row hidden" hidden>Hidden</div></div>',
            "entry_scope_no_candidate_entries",
            "entry scope selector matched no visible candidate entry containers",
        ),
        (
            '<div class="ranking"><p>Visible text without row containers</p></div>',
            "entry_scope_no_candidate_entries",
            "entry scope selector matched no visible candidate entry containers",
        ),
    ),
)
def test_declared_scope_failures_have_specific_reason_codes_and_messages(
    html: str, reason_code: str, reason: str
) -> None:
    verdict = evaluate_captured_source_evidence(
        _evidence(f"<h1>Weekly ranking</h1>{html}", entry_scope_selector=".ranking > .ranking-row")
    )

    assert verdict.reason_code == reason_code
    assert verdict.reason == reason


def test_declared_scope_with_visible_unlinked_rows_reaches_scoped_unlinked_gate() -> None:
    html = '<h1>Weekly ranking</h1><div class="ranking"><div class="ranking-row">1 Book One Jane Author</div></div>'
    verdict = evaluate_captured_source_evidence(_evidence(html, entry_scope_selector=".ranking > .ranking-row"))
    assert verdict.reason_code == "entry_scope_unlinked_entries"
    assert verdict.reason == "declared entry scope exposes no server-rendered linked list entries"


def test_landmark_free_chrome_outside_scope_cannot_trigger_generic_scope_failures() -> None:
    html = (
        '<h1>Weekly ranking</h1><div class="site-menu"><ul><li>'
        '<a href="/menu-item">Menu item</a></li></ul></div>'
        '<div class="ranking">Visible ranking text<div class="ranking-row">No link</div></div>'
    )
    verdict = evaluate_captured_source_evidence(_evidence(html, entry_scope_selector=".ranking > .ranking-row"))
    assert verdict.reason_code == "entry_scope_unlinked_entries"
    assert verdict.reason == "declared entry scope exposes no server-rendered linked list entries"


def test_declared_scope_linked_entry_without_author_uses_scoped_reason() -> None:
    html = (
        '<h1>Weekly ranking</h1><div class="ranking"><div class="ranking-row">'
        '<a href="/books/one">Book One</a></div></div>'
    )
    verdict = evaluate_captured_source_evidence(_evidence(html, entry_scope_selector=".ranking > .ranking-row"))
    assert verdict.reason_code == "entry_scope_missing_authors"
    assert verdict.reason == "declared entry scope bounded entry 'Book One' has no visible primary author"


def test_publishnews_fixture_reaches_scoped_unlinked_gate_with_both_selectors() -> None:
    evidence = json.loads(
        (ROOT / "tests/fixtures/issue-334-captured-source-evidence/publishnews-visible-title.json").read_text(
            encoding="utf-8"
        )
    )
    evidence = evidence_from_mapping(
        {
            **evidence,
            "visible_title_selector": (
                ".pn-colunas-06x07 > .pn-colunas-corpo > .pn-editoria > .pn-editoria-cabeca > .pn-editoria-titulo"
            ),
            "entry_scope_selector": (".pn-colunas-06x07 > .pn-colunas-corpo > .pn-editoria > .pn-ranking-livro"),
        }
    )

    verdict = evaluate_captured_source_evidence(evidence)
    assert verdict.reason_code == "entry_scope_unlinked_entries"
    assert verdict.reason == "declared entry scope exposes no server-rendered linked list entries"
    assert verdict.detected_heading == (
        "Lista Nielsen-PublishNews de Mais Vendidos de Não Ficção Trade de 03/08/2026 a 09/08/2026"
    )
    assert verdict.bounded_entry_count == 0


class _Response:
    def __init__(self, status: int, body: bytes = b"", url: str = SOURCE_URL) -> None:
        self.status = status
        self.body = body
        self.url = url

    def geturl(self) -> str:
        return self.url

    def read(self, _size: int) -> bytes:
        return self.body

    def close(self) -> None:
        pass


class _Transport:
    def __init__(self, body: bytes) -> None:
        self.responses = [_Response(200), _Response(200, body)]
        self.calls = 0

    def __call__(self, _request: Any, timeout: float) -> _Response:
        del timeout
        self.calls += 1
        return self.responses.pop(0)


class _NoNetwork:
    def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("network access was attempted")


def _probe_html() -> bytes:
    return (
        b'<h1>Weekly ranking</h1><div class="ranking"><div class="ranking-row">'
        b'<a href="/books/one">Book One</a><span class="author">Jane Author</span>'
        b"</div></div>"
    )


def _default_probe_html() -> bytes:
    return (
        b'<h1>Weekly ranking</h1><ul><li><a href="/books/one">Book One</a>'
        b'<span class="author">Jane Author</span></li></ul>'
    )


def test_probe_propagates_scope_to_report_duplicate_and_capture_without_ledger_schema_change(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    captures = tmp_path / "captures"
    drafts = tmp_path / "drafts"
    selector = ".ranking > .ranking-row"
    report, code = run_source_candidate_probe(
        [SOURCE_URL],
        entry_scope_selectors=[selector],
        ledger_path=ledger,
        keep_captures=captures,
        feasibility_dir=drafts,
        opener=_Transport(_probe_html()),
    )

    assert code == 0
    assert report["candidates"][0]["entry_scope_selector"] == selector
    capture = next(captures.glob("*.json"))
    assert json.loads(capture.read_text(encoding="utf-8"))["entry_scope_selector"] == selector
    assert "entry_scope_selector" not in json.loads(ledger.read_text(encoding="utf-8"))["entries"][0]

    duplicate, duplicate_code = run_source_candidate_probe(
        [SOURCE_URL],
        entry_scope_selectors=[selector],
        ledger_path=ledger,
        feasibility_dir=drafts,
        opener=_NoNetwork(),
    )
    assert duplicate_code == 0
    assert duplicate["candidates"][0]["entry_scope_selector"] == selector


def test_default_probe_report_omits_entry_scope_selector(tmp_path: Path) -> None:
    report, code = run_source_candidate_probe(
        [SOURCE_URL],
        ledger_path=tmp_path / "ledger.json",
        feasibility_dir=tmp_path / "drafts",
        opener=_Transport(_default_probe_html()),
    )

    assert code == 0
    assert "entry_scope_selector" not in report["candidates"][0]


def test_default_retained_capture_omits_entry_scope_selector(tmp_path: Path) -> None:
    captures = tmp_path / "captures"
    report, code = run_source_candidate_probe(
        [SOURCE_URL],
        ledger_path=tmp_path / "ledger.json",
        feasibility_dir=tmp_path / "drafts",
        keep_captures=captures,
        opener=_Transport(_default_probe_html()),
    )

    assert code == 0
    assert report["candidates"][0]["outcome"] == "accept"
    capture = json.loads(next(captures.glob("*.json")).read_text(encoding="utf-8"))
    assert "entry_scope_selector" not in capture


def test_default_duplicate_probe_report_omits_entry_scope_selector(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    run_source_candidate_probe(
        [SOURCE_URL], ledger_path=ledger, feasibility_dir=tmp_path / "drafts", opener=_Transport(_default_probe_html())
    )
    report, code = run_source_candidate_probe(
        [SOURCE_URL], ledger_path=ledger, feasibility_dir=tmp_path / "drafts", opener=_NoNetwork()
    )

    assert code == 0
    assert "entry_scope_selector" not in report["candidates"][0]


def test_entry_scope_selectors_align_per_url_and_default_to_absent(tmp_path: Path) -> None:
    second_url = "https://example.test/other-ranking"
    transport = _Transport(_probe_html())
    transport.responses = [
        _Response(200, url=SOURCE_URL),
        _Response(200, _probe_html(), SOURCE_URL),
        _Response(200, url=second_url),
        _Response(200, _probe_html(), second_url),
    ]

    report, code = run_source_candidate_probe(
        [SOURCE_URL, second_url],
        entry_scope_selectors=[".ranking > .ranking-row"],
        ledger_path=tmp_path / "ledger.json",
        feasibility_dir=tmp_path / "drafts",
        opener=transport,
    )

    by_url = {candidate["url"]: candidate for candidate in report["candidates"]}
    assert code == 0
    assert by_url[SOURCE_URL]["outcome"] == "accept"
    assert by_url[second_url]["reason_code"] == "unbounded_entries"
    assert "entry_scope_selector" not in by_url[second_url]


def test_probe_rejects_malformed_scope_before_network_or_storage(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    with pytest.raises(ValueError, match="entry scope selector"):
        run_source_candidate_probe(
            [SOURCE_URL],
            entry_scope_selectors=[".ranking"],
            ledger_path=ledger,
            opener=_NoNetwork(),
        )
    assert not ledger.exists()


def test_probe_cli_forwards_entry_scope_selector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    import autoscout.source_probe as source_probe

    monkeypatch.setattr(source_probe, "urlopen", _Transport(_probe_html()))
    selector = ".ranking > .ranking-row"
    code = main(
        [
            "probe-source-candidate",
            "--url",
            SOURCE_URL,
            "--entry-scope-selector",
            selector,
            "--ledger",
            str(tmp_path / "ledger.json"),
            "--feasibility-dir",
            str(tmp_path / "drafts"),
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["candidates"][0]["entry_scope_selector"] == selector


def test_ranking_contract_carries_layout_declarations_and_grounds_custom_statement() -> None:
    source = {
        "name": "Example ranking",
        "family": "bestseller/ranking",
        "url": SOURCE_URL,
        "language": "en",
        "metadata_ranking_table": True,
        "entry_scope_selector": ".ranking > .ranking-row",
        "ranking_source_statement": "Source: Nielsen BookScan",
    }
    contract = extraction_contract(source)
    assert contract["source_metadata"]["entry_scope_selector"] == ".ranking > .ranking-row"
    assert contract["source_metadata"]["ranking_source_statement"] == "Source: Nielsen BookScan"
    assert "Source: Nielsen BookScan" in " ".join(contract["rules"])

    html = (
        '<div class="ranking"><div class="ranking-row"><span>1</span>'
        '<a href="/books/one">Book One</a><span>Jane Author</span><span>978-1-234</span>'
        "</div></div>Source: Nielsen BookScan week 2026-08-17"
    )
    mention = {
        "title": "Book One",
        "primary_authors": ["Jane Author"],
        "isbn": "978-1-234",
        "current_rank": "1",
        "source_week": "2026-08-17",
        "source_url": SOURCE_URL,
        "evidence_url": SOURCE_URL,
        "signal_type": "bestseller/ranking",
        "signal_strength": "top ranking",
        "evidence_note": "Rank 1 for the week 2026-08-17.",
    }
    assert extraction_contract_violation(mention, mention, source, {"html": html}) == ""

    scoped_failure = extraction_contract_violation(
        mention,
        mention,
        source,
        {"html": html.replace("Jane Author", "Other Author")},
    )
    assert scoped_failure == "rank, book metadata, or source week is not grounded in one declared bounded entry row"

    legacy_source = {
        key: value for key, value in source.items() if key not in {"entry_scope_selector", "ranking_source_statement"}
    }
    legacy_failure = extraction_contract_violation(
        mention,
        mention,
        legacy_source,
        {"html": html.replace("Nielsen BookScan", "Circana BookScan").replace("Jane Author", "Other Author")},
    )
    assert (
        legacy_failure == "ranking table rank, book metadata, or source week is not grounded in one current table row"
    )


def test_config_rejects_blank_and_malformed_layout_declarations() -> None:
    config = load_validated_config(ROOT / "configs/current-weekly-portfolio.json")
    source = dict(next(item for item in config["sources"] if item.get("metadata_ranking_table") is True))
    invalid = {
        **config,
        "sources": [{**source, "entry_scope_selector": ".ranking", "ranking_source_statement": "  "}],
    }
    errors = validate_config(invalid)
    assert any("entry scope selector" in error and "a scope and entry class" in error for error in errors)
    assert any("ranking_source_statement" in error and "nonblank" in error for error in errors)


def test_declared_div_row_grounding_does_not_require_a_link_anchor() -> None:
    html = (
        '<div class="ranking"><div class="ranking-row">'
        "1 Book One Jane Author ISBN 978-1-234"
        "</div></div>Source: Nielsen BookScan week 2026-08-17"
    )

    assert ranking_mention_is_grounded(
        html,
        title="Book One",
        primary_author="Jane Author",
        isbn="978-1-234",
        current_rank="1",
        source_week="2026-08-17",
        entry_scope_selector=".ranking > .ranking-row",
        ranking_source_statement="Source: Nielsen BookScan",
    )


def test_declared_div_rows_reject_metadata_spread_across_containers() -> None:
    html = (
        '<div class="ranking"><div class="ranking-row"><span>1</span>'
        '<a href="/books/one">Book One</a></div>'
        '<div class="ranking-row"><span class="author">Jane Author</span>'
        "<span>ISBN 978-1-234</span></div></div>"
        "Source: Nielsen BookScan week 2026-08-17"
    )

    assert not ranking_mention_is_grounded(
        html,
        title="Book One",
        primary_author="Jane Author",
        isbn="978-1-234",
        current_rank="1",
        source_week="2026-08-17",
        entry_scope_selector=".ranking > .ranking-row",
        ranking_source_statement="Source: Nielsen BookScan",
    )
