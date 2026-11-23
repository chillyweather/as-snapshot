"""Dated frontlist cards keep navigation and neighbouring books separate (#901, #902)."""

from __future__ import annotations

from typing import Any

import pytest

from autoscout.extraction_completeness import capture_listing_row_count
from autoscout.nuevoslibros_frontlist import FAMILY
from autoscout.nuevoslibros_frontlist import frontlist_cards
from autoscout.replay_fixtures import build_replay_fixture_pair
from autoscout.source_collectors.agent_model_input import model_bounded_page_data
from autoscout.source_collectors.base import CollectorRunError
from autoscout.source_collectors.base import extraction_contract_violation
from autoscout.source_collectors.contract import extraction_contract
from autoscout.source_feasibility import evaluate_source_feasibility
from autoscout.source_feasibility import retained_summary_samples
from autoscout.source_page_identity import validate_page_shape

URL = "https://nuevoslibros.com/genero/ensayo/"
SOURCE: dict[str, Any] = {
    "url": URL,
    "contract_family": FAMILY,
    "language": "es",
    "family": "media/curated list",
    "signal_family": "media/curated list",
    "expected_signal_strength": "major media list",
    "eligible_candidate_category": "adult nonfiction",
    "expected_page_heading": "Novedades de ensayo",
}
HTML = """<body data-genero="ensayo">
<aside><ul><li><a href="/editorial/example/">Example</a></li></ul></aside>
<main class="main"><h1>Novedades de ensayo</h1>
<section class="week"><h2>2–8 NOV 2026</h2><div class="grid">
<article class="book" data-fecha="2026-11-04"><a class="cover-link" href="/libro/uno/"><img alt="cover"></a>
<p class="caption"><a class="t" href="/libro/uno/">Un mundo común</a>
<a href="/autor/ana/">Ana Pérez</a>, <a href="/autor/luis/">Luis García</a>
· <a href="/editorial/example/">Example</a> · <a href="/genero/ensayo/">ensayo</a></p></article>
</div></section>
<section class="week"><h2>26 OCT–1 NOV 2026</h2><div class="grid">
<article class="book" data-fecha="2026-10"><p class="caption">
<a class="t" href="/libro/dos/">Otro mundo</a><a href="/autor/maria/">María Ruiz</a></p></article>
</div></section></main></body>"""


def evidence(markup: str = HTML) -> dict[str, Any]:
    return {
        "source_url": URL,
        "source_name": "NuevosLibros test",
        "source_page": URL,
        "resolved_url": URL,
        "head_status": 200,
        "get_status": 200,
        "html": markup,
        "contract_family": FAMILY,
        "collection_mode": "source-candidate-probe",
        # Every NuevosLibros capture carries its reference date (#993); both
        # cards of this page fall in the recent window of that date.
        "reference_date": "2026-11-07",
    }


def entry() -> dict[str, Any]:
    return {
        "title": "Un mundo común",
        "primary_authors": ["Ana Pérez", "Luis García"],
        "publication_year": 2026,
        "source_url": URL,
        "evidence_url": "https://nuevoslibros.com/libro/uno/",
        "signal_type": "media/curated list",
        "signal_strength": "major media list",
        "candidate_category": "adult nonfiction",
        "evidence_note": "Dated frontlist entry.",
        "english_title": "A Common World",
        "search_translation": "Общий мир",
        "author_transliteration_note": "Ана Перес / Луис Гарсия",
    }


def test_opt_in_contract_counts_cards_across_weeks_without_navigation() -> None:
    assert not evaluate_source_feasibility({}, evidence()).accepted
    verdict = evaluate_source_feasibility(SOURCE, evidence())
    assert verdict.accepted
    assert verdict.bounded_entry_count == 2
    assert verdict.samples[0].author == "Ana Pérez / Luis García"
    assert capture_listing_row_count(SOURCE, evidence()) == 2


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ('data-genero="ensayo"', 'data-genero="novela"'),
        ('class="main"', 'class="other"'),
        ('data-fecha="2026-11-04"', 'data-fecha="2026-02-30"'),
        ('data-fecha="2026-11-04"', 'data-fecha=""'),
        ('class="t" href="/libro/uno/"', 'class="t" href="https://other.test/libro/uno/"'),
        ('<a href="/autor/maria/">María Ruiz</a>', ""),
        ('href="/libro/dos/"', 'href="/libro/uno/"'),
        ("Novedades de ensayo", "Novedades de novela"),
        ("</p></article>", "</article>"),
    ],
)
def test_changed_or_ambiguous_visible_cards_refuse_the_page(before: str, after: str) -> None:
    page = evidence(HTML.replace(before, after, 1))
    assert not evaluate_source_feasibility(SOURCE, page).accepted
    assert validate_page_shape(SOURCE, page)


def test_hidden_cards_never_contribute_metadata() -> None:
    page = HTML.replace(
        '<article class="book" data-fecha="2026-10">', '<article hidden class="book" data-fecha="2026-10">'
    )
    assert len(frontlist_cards(page, URL)) == 1


@pytest.mark.parametrize("status", [403, 429])
def test_http_refusals_still_win_over_readable_cards(status: int) -> None:
    page = {**evidence(), "head_status": status}
    assert not evaluate_source_feasibility(SOURCE, page).accepted


def test_wrong_redirect_is_refused() -> None:
    page = {**evidence(), "resolved_url": "https://nuevoslibros.com/genero/historia/"}
    assert not evaluate_source_feasibility(SOURCE, page).accepted


def test_mention_requires_the_card_authors_year_and_exact_list_helpers() -> None:
    mention = entry()
    assert extraction_contract_violation(mention, mention, SOURCE, evidence()) == ""
    for changed in [
        {"primary_authors": ["Ana Pérez"]},
        {"primary_authors": ["María Ruiz"]},
        {"evidence_url": "https://nuevoslibros.com/libro/dos/"},
        {"publication_year": 2025},
        {"signal_strength": "top ranking"},
    ]:
        invalid = {**mention, **changed}
        assert extraction_contract_violation(invalid, invalid, SOURCE, evidence())


@pytest.mark.parametrize("title", ["Stalin", "Moonwalk"])
def test_a_title_that_reads_the_same_in_english_keeps_its_book(title: str) -> None:
    # The 2026-11-03 run dropped "Stalin" and "Moonwalk" because english_title equalled the title.
    page = evidence(HTML.replace(">Un mundo común<", f">{title}<", 1))
    mention = {**entry(), "title": title, "english_title": title, "search_translation": "Книга"}
    assert extraction_contract_violation(mention, mention, SOURCE, page) == ""


@pytest.mark.parametrize("year", ["2026", " 2026 ", None])
def test_year_written_as_digits_matches_the_card(year: object) -> None:
    # The 2026-11-03 run lost every NuevosLibros Biografía book: the extractor wrote "2026".
    mention = {**entry(), "publication_year": year}
    assert extraction_contract_violation(mention, mention, SOURCE, evidence()) == ""


@pytest.mark.parametrize("year", ["2025", "", "twenty", True, 2026.5])
def test_other_years_still_differ_from_the_card(year: object) -> None:
    mention = {**entry(), "publication_year": year}
    assert extraction_contract_violation(mention, mention, SOURCE, evidence()) == (
        "frontlist publication_year differs from its stated release date"
    )


def test_model_input_contains_all_card_metadata_and_refuses_truncation() -> None:
    page, debug = model_bounded_page_data(evidence(), 5000, source=SOURCE)
    assert "Un mundo común" in page["html"]
    assert "Otro mundo" in page["html"]
    assert "2026-11-04" in page["html"]
    assert "/editorial/" not in page["html"]
    assert debug["model_input_truncated"] is False
    with pytest.raises(CollectorRunError, match="budget"):
        model_bounded_page_data(evidence(), 20, source=SOURCE)
    contract = extraction_contract(SOURCE)
    assert "english_title" in contract["required_fields"]


def test_replay_keeps_real_dates_and_remains_accepted() -> None:
    captured = evidence()
    verdict = evaluate_source_feasibility(SOURCE, captured)
    helpers = {
        "version": 1,
        "source_url": URL,
        "language": "es",
        "entries": [
            {
                "title": sample.title,
                "author": sample.author,
                "evidence_url": sample.url,
                "english_title": "A Common World" if index == 0 else "Another World",
                "search_translation": "Общий мир" if index == 0 else "Другой мир",
                "author_transliteration_note": "Ана Перес / Луис Гарсия" if index == 0 else "Мария Руис",
            }
            for index, sample in enumerate(verdict.samples)
        ],
    }
    captured.update(
        samples=retained_summary_samples(verdict), bounded_entry_count=2, detected_heading=verdict.detected_heading
    )
    pair = build_replay_fixture_pair(
        url=URL,
        slug="frontlist",
        evidence=captured,
        signal_family="media/curated list",
        signal_strength="major media list",
        candidate_category="adult nonfiction",
        language="es",
        contract_family=FAMILY,
        helper_metadata=helpers,
    )
    cards = frontlist_cards(pair.page["html"], URL)
    assert [card.release_date for card in cards] == ["2026-11-04", "2026-10"]
    assert validate_page_shape(SOURCE, pair.page) == ""
    for mention in pair.extraction["mentions"]:
        assert extraction_contract_violation(mention, mention, SOURCE, pair.page) == ""


def test_real_backend_transport_can_build_prompt_from_prepared_input(tmp_path: Any, monkeypatch: Any) -> None:
    from autoscout.source_collectors.agent_backends import RealAgentExtractionBackend
    from autoscout.source_collectors.contract import agent_extraction_prompt

    class PromptTransport:
        def extract(self, **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
            prompt = agent_extraction_prompt(kwargs["source"], kwargs["page_data"], kwargs["max_chars"])
            assert "Un mundo común" in prompt
            assert "Otro mundo" in prompt
            assert "/editorial/" not in prompt
            return {"mentions": [entry()]}, {}

    monkeypatch.setenv("AUTOSCOUT_TEST_KEY", "test-only")
    source = {**SOURCE, "enable_real_backend": True, "credential_env": "AUTOSCOUT_TEST_KEY"}
    result, _ = RealAgentExtractionBackend(transport=PromptTransport()).extract(
        tmp_path / "config.json", source, evidence()
    )
    assert result["mentions"] == [entry()]


@pytest.mark.parametrize("limit", [5000, 20])
def test_weekly_codex_gets_bounded_cards_without_changing_grounding_capture(
    tmp_path: Any, monkeypatch: Any, limit: int
) -> None:
    import json

    from autoscout.codex_recovery import CodexCliExtractionAgent
    from autoscout.codex_recovery import ExtractionStatus
    from autoscout.source_metadata import portable_source_metadata

    capture = tmp_path / "capture.json"
    capture.write_text(json.dumps({**evidence(), "text": "raw navigation and prose"}))
    original = capture.read_bytes()
    calls = []

    def run(self: Any, command: Any, isolated: Any, response_path: Any, timeout: Any) -> tuple[int, str]:
        page = json.loads((isolated / "capture.json").read_text())
        assert "Un mundo común" in page["html"] and "Otro mundo" in page["html"]
        assert "/editorial/" not in page["html"]
        assert page["text"] == ""
        assert len(page["html"]) <= limit
        calls.append(command)
        response_path.write_text(json.dumps({"entries": [entry()]}))
        return 0, ""

    monkeypatch.setattr(CodexCliExtractionAgent, "_run_codex", run)
    item = {
        "source_url": URL,
        "source_metadata": portable_source_metadata({**SOURCE, "max_model_input_chars": limit}),
    }
    result = CodexCliExtractionAgent(timeout_seconds=5).extract(item, capture, tmp_path / "artifact.json")
    assert capture.read_bytes() == original
    assert result.status == (ExtractionStatus.PRODUCED if limit == 5000 else ExtractionStatus.UNAVAILABLE)
    assert len(calls) == (1 if limit == 5000 else 0)


def test_a_collection_pass_parses_the_frontlist_once_for_every_mention(monkeypatch: pytest.MonkeyPatch) -> None:
    from autoscout import nuevoslibros_frontlist
    from autoscout.source_page_identity import ParsedCapture

    parses: list[str] = []

    class CountingParser(nuevoslibros_frontlist._Parser):
        def feed(self, data: str) -> None:
            parses.append(self.source_url)
            super().feed(data)

    monkeypatch.setattr(nuevoslibros_frontlist, "_Parser", CountingParser)
    nuevoslibros_frontlist._frontlist_cards.cache_clear()
    nuevoslibros_frontlist.linked_html.cache_clear()
    page = evidence()
    parsed = ParsedCapture(HTML, URL)
    second = {
        **entry(),
        "title": "Otro mundo",
        "primary_authors": ["María Ruiz"],
        "evidence_url": "https://nuevoslibros.com/libro/dos/",
    }
    for mention in [entry(), second, entry()]:
        assert extraction_contract_violation(mention, mention, SOURCE, page, parsed=parsed) == ""
    assert parses == [URL]


def _real_source(monkeypatch: Any, output_tokens: int) -> dict[str, Any]:
    monkeypatch.setenv("AUTOSCOUT_TEST_KEY", "test-only")
    return {
        **SOURCE,
        "enable_real_backend": True,
        "credential_env": "AUTOSCOUT_TEST_KEY",
        "model_max_output_tokens": output_tokens,
    }


def test_live_api_splits_a_frontlist_the_output_budget_cannot_answer_at_once(tmp_path: Any, monkeypatch: Any) -> None:
    from autoscout.source_collectors.agent_backends import RealAgentExtractionBackend
    from autoscout.source_collectors.agent_model_input import FRONTLIST_OUTPUT_TOKENS_PER_CARD

    second = {**entry(), "title": "Otro mundo", "primary_authors": ["María Ruiz"]}
    second["evidence_url"] = "https://nuevoslibros.com/libro/dos/"
    seen: list[list[str]] = []

    class BatchTransport:
        def extract(self, **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
            cards = frontlist_cards(kwargs["page_data"]["html"], URL)
            seen.append([card.title for card in cards])
            return {"mentions": [entry() if card.title == "Un mundo común" else second for card in cards]}, {}

    source = _real_source(monkeypatch, FRONTLIST_OUTPUT_TOKENS_PER_CARD)
    result, debug = RealAgentExtractionBackend(transport=BatchTransport()).extract(
        tmp_path / "config.json", source, evidence()
    )
    assert seen == [["Un mundo común"], ["Otro mundo"]]
    assert result == {"mentions": [entry(), second]}
    assert debug["model_request_count"] == 2

    seen.clear()
    source = _real_source(monkeypatch, 2 * FRONTLIST_OUTPUT_TOKENS_PER_CARD)
    result, debug = RealAgentExtractionBackend(transport=BatchTransport()).extract(
        tmp_path / "config.json", source, evidence()
    )
    assert seen == [["Un mundo común", "Otro mundo"]]
    assert "model_request_count" not in debug


@pytest.mark.parametrize("answer", [{}, {"error": "failed"}, {"mentions": "not a list"}, "not json rows"])
def test_a_frontlist_batch_without_a_mentions_list_fails_the_source(
    tmp_path: Any, monkeypatch: Any, answer: Any
) -> None:
    from autoscout.source_collectors.agent_backends import RealAgentExtractionBackend
    from autoscout.source_collectors.agent_model_input import FRONTLIST_OUTPUT_TOKENS_PER_CARD

    answers = [{"mentions": [entry()]}, answer]

    class MalformedTransport:
        def extract(self, **kwargs: Any) -> tuple[Any, dict[str, Any]]:
            return answers.pop(0), {}

    source = _real_source(monkeypatch, FRONTLIST_OUTPUT_TOKENS_PER_CARD)
    with pytest.raises(CollectorRunError) as raised:
        RealAgentExtractionBackend(transport=MalformedTransport()).extract(tmp_path / "config.json", source, evidence())
    assert raised.value.debug_metadata["safe_failure_status"] == "model_output_malformed"


def test_an_explicitly_empty_frontlist_batch_still_merges(tmp_path: Any, monkeypatch: Any) -> None:
    from autoscout.source_collectors.agent_backends import RealAgentExtractionBackend
    from autoscout.source_collectors.agent_model_input import FRONTLIST_OUTPUT_TOKENS_PER_CARD

    answers: list[dict[str, Any]] = [{"mentions": [entry()]}, {"entries": []}]

    class EmptyTransport:
        def extract(self, **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
            return answers.pop(0), {}

    source = _real_source(monkeypatch, FRONTLIST_OUTPUT_TOKENS_PER_CARD)
    result, _ = RealAgentExtractionBackend(transport=EmptyTransport()).extract(
        tmp_path / "config.json", source, evidence()
    )
    assert result == {"mentions": [entry()]}


@pytest.mark.parametrize("answer", [{"status": "blocked", "mentions": []}])
def test_one_refused_frontlist_batch_answers_for_the_whole_source(
    tmp_path: Any, monkeypatch: Any, answer: dict[str, Any]
) -> None:
    from autoscout.source_collectors.agent_backends import RealAgentExtractionBackend
    from autoscout.source_collectors.agent_model_input import FRONTLIST_OUTPUT_TOKENS_PER_CARD

    answers = [{"mentions": [entry()]}, answer]

    class RefusingTransport:
        def extract(self, **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
            return answers.pop(0), {}

    source = _real_source(monkeypatch, FRONTLIST_OUTPUT_TOKENS_PER_CARD)
    result, _ = RealAgentExtractionBackend(transport=RefusingTransport()).extract(
        tmp_path / "config.json", source, evidence()
    )
    assert result == answer
