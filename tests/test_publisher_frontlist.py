"""Publisher title/author cards stay grounded across probe, replay and extraction (#936, #937)."""

from typing import Any

import pytest

from autoscout.extraction_completeness import capture_listing_row_count
from autoscout.publisher_frontlist import BEACON
from autoscout.publisher_frontlist import FAMILY
from autoscout.publisher_frontlist import VERSO
from autoscout.publisher_frontlist import frontlist_cards
from autoscout.publisher_frontlist import replay_html
from autoscout.replay_fixtures import build_replay_fixture_pair
from autoscout.source_collectors.agent_model_input import model_bounded_page_data
from autoscout.source_collectors.base import extraction_contract_violation
from autoscout.source_feasibility import evaluate_source_feasibility
from autoscout.source_feasibility import retained_summary_samples

VERSO_HTML = """<main><h1>Forthcoming</h1><article class="c-product-card">
<a class="c-product-card__product-link" href="/en-gb/products/1-common-world"><header>
<h3 class="c-product-card__title">A Common World</h3></header></a>
<footer><p class="c-product-card__byline">by <a href="/en-gb/blogs/authors/ana">Ana Perez</a>
and <a href="/en-gb/blogs/authors/luis">Luis Garcia</a></p></footer></article></main>"""
BEACON_HTML = """<h1>Forthcoming</h1><table><tr><td><div class="SearchItem">
<a href="/A-Common-World-P1.aspx"><img class="Thumbnail" alt="A Common World"></a>
<span><span class="authorbold">Author: </span><span class="author"><a href="/author">Ana Perez</a></span>
<span class="authorbold">Foreword by: </span><span class="author"><a href="/foreword">Luis Garcia</a></span></span>
</td></tr></table>"""


def source(url: str) -> dict[str, Any]:
    return dict(
        url=url,
        contract_family=FAMILY,
        language="en",
        family="media/curated list",
        signal_family="media/curated list",
        expected_signal_strength="minor mention",
        eligible_candidate_category="adult nonfiction",
        expected_page_heading="Forthcoming",
    )


def evidence(url: str, markup: str) -> dict[str, Any]:
    return dict(
        source_url=url,
        source_name="Publisher test",
        source_page=url,
        resolved_url=url,
        html=markup,
        head_status=200,
        get_status=200,
        contract_family=FAMILY,
        collection_mode="source-candidate-probe",
    )


@pytest.mark.parametrize(
    ("url", "markup", "authors"),
    [(VERSO, VERSO_HTML, ["Ana Perez", "Luis Garcia"]), (BEACON, BEACON_HTML, ["Ana Perez"])],
)
def test_full_contract_round_trip(url: str, markup: str, authors: list[str]) -> None:
    src = source(url)
    page = evidence(url, markup)
    verdict = evaluate_source_feasibility(src, page)
    assert verdict.accepted
    assert verdict.bounded_entry_count == 1
    assert frontlist_cards(markup, url)[0].authors == tuple(authors)
    assert capture_listing_row_count(src, page) == 1
    compacted, debug = model_bounded_page_data(page, 5000, source=src)
    assert debug["model_input_truncated"] is False
    assert frontlist_cards(compacted["html"], url)[0].authors == tuple(authors)
    helpers = dict(
        version=1,
        source_url=url,
        language="en",
        entries=[
            dict(
                title=s.title,
                author=s.author,
                evidence_url=s.url,
                english_title=s.title,
                search_translation="Общий мир",
                author_transliteration_note="Ана Перес / Луис Гарсия",
            )
            for s in verdict.samples
        ],
    )
    page.update(samples=retained_summary_samples(verdict), bounded_entry_count=1, detected_heading="Forthcoming")
    pair = build_replay_fixture_pair(
        url=url,
        slug="publisher-test",
        evidence=page,
        signal_family="media/curated list",
        signal_strength="minor mention",
        candidate_category="adult nonfiction",
        language="en",
        contract_family=FAMILY,
        helper_metadata=helpers,
    )
    for mention in pair.extraction["mentions"]:
        assert mention["primary_authors"] == authors
        assert mention.get("publication_year") is None
        assert extraction_contract_violation(mention, mention, src, pair.page) == ""
        changed = {**mention, "publication_year": 2027}
        assert extraction_contract_violation(changed, changed, src, pair.page)
        changed = {**mention, "primary_authors": ["Someone Else"]}
        assert extraction_contract_violation(changed, changed, src, pair.page)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("A Common World", ""),
        ("/en-gb/products/1-common-world", "https://evil.test/book"),
        ("Ana Perez", ""),
        ("Forthcoming", "Other"),
        ("</footer>", ""),
        ("<article class=", "<article hidden class="),
    ],
)
def test_changed_cards_refuse_instead_of_lending_metadata(before: str, after: str) -> None:
    assert not evaluate_source_feasibility(source(VERSO), evidence(VERSO, VERSO_HTML.replace(before, after))).accepted


def test_duplicate_and_oversized_lists_refuse() -> None:
    card = VERSO_HTML.split("<article", 1)[1].split("</article>")[0]
    with pytest.raises(ValueError, match="repeats"):
        frontlist_cards("<main><h1>Forthcoming</h1>" + ("<article" + card + "</article>") * 2 + "</main>", VERSO)
    with pytest.raises(ValueError, match="100"):
        frontlist_cards(
            "<main><h1>Forthcoming</h1>"
            + "".join("<article" + card.replace("1-common-world", f"{i}-world") + "</article>" for i in range(101))
            + "</main>",
            VERSO,
        )


def test_merchandise_is_explicitly_excluded_and_replay_is_bounded() -> None:
    merch = '<article class="c-product-card"><div class="c-product-card__image-wrapper--merchandise"></div></article>'
    assert len(frontlist_cards(VERSO_HTML.replace("</main>", merch + "</main>"), VERSO)) == 1
    assert "merchandise" not in replay_html(VERSO_HTML, VERSO, [frontlist_cards(VERSO_HTML, VERSO)[0].url])


@pytest.mark.parametrize("status", [403, 429])
def test_http_refusals_win(status: int) -> None:
    assert not evaluate_source_feasibility(
        source(VERSO), {**evidence(VERSO, VERSO_HTML), "head_status": status}
    ).accepted


def test_verso_editor_is_not_a_primary_author() -> None:
    edited = VERSO_HTML.replace(
        "</footer>", '<p class="c-product-card__byline">Edited by <a href="/editor">Another Person</a></p></footer>'
    )
    assert frontlist_cards(edited, VERSO)[0].authors == ("Ana Perez", "Luis Garcia")
