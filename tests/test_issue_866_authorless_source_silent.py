"""Silent peers neither veto identity evidence nor gain admission (#866)."""

from dataclasses import replace

import pytest

from autoscout.grounded_description import REASON_SOURCE_IDENTITY_DISAGREEMENT
from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import FetchedSource
from autoscout.grounded_description import evaluate_grounding_gate

TARGET = DurableTarget("herois", "Heróis por acaso", ("Octavio Guedes",), {"isbn": "9786586339598"})


def source(owner: str, *, authors: tuple[str, ...] = ()) -> FetchedSource:
    return FetchedSource(
        url=f"https://{owner}.example/book",
        source_kind="review",
        source_family="review",
        owner_id=owner,
        source_language="pt",
        title=TARGET.title,
        primary_authors=authors,
        subject_text="The book traces improbable characters in Brazilian history.",
        checked_date="2026-10-27",
    )


def silent() -> FetchedSource:
    # Different text, compatible subject: this must not use #483's exception.
    return replace(source("silent"), subject_text="A history of Brazil and its unexpected characters.")


@pytest.mark.parametrize("evidence", ["authors", "subject", "headline", "isbn"])
@pytest.mark.parametrize("reverse", [False, True])
def test_one_evidenced_page_and_silent_peer_accepts_only_evidenced_page(evidence: str, reverse: bool) -> None:
    named = source("evidenced")
    if evidence == "authors":
        named = replace(named, primary_authors=TARGET.primary_authors)
    elif evidence == "subject":
        named = replace(named, subject_text="Octavio Guedes traces improbable characters in Brazilian history.")
    elif evidence == "headline":
        named = replace(named, title="'Heróis por acaso': Octavio Guedes lança livro sobre a história do Brasil")
    else:
        named = replace(named, isbn="9786586339598")
    pages = [named, silent()]
    result = evaluate_grounding_gate(TARGET, pages[::-1] if reverse else pages)
    assert result.accepted
    assert result.reason_code == ""
    assert [item.canonical_url for item in result.accepted_sources] == [named.url]
    assert all(item.reason_code != REASON_SOURCE_IDENTITY_DISAGREEMENT for item in result.rejected_sources)


def test_different_author_still_conflicts_even_with_target_author_in_headline() -> None:
    other = replace(source("other", authors=("Someone Else",)), title=f"{TARGET.title} by Octavio Guedes")
    result = evaluate_grounding_gate(TARGET, [source("named", authors=TARGET.primary_authors), other])
    assert not result.accepted
    assert result.reason_code == REASON_SOURCE_IDENTITY_DISAGREEMENT


def test_author_in_page_title_is_identity_evidence() -> None:
    page = replace(source("headline"), title="'Heróis por acaso': Octavio Guedes lança livro")
    assert evaluate_grounding_gate(TARGET, [page]).accepted


@pytest.mark.parametrize("headline_name", ["Guedess", "Guedesville", "Guédes"])
def test_headline_author_matching_keeps_word_boundaries_and_accents(headline_name: str) -> None:
    page = replace(source("headline"), title=f"{TARGET.title}: {headline_name} lança livro")
    result = evaluate_grounding_gate(TARGET, [page, silent()])
    assert not result.accepted
    assert result.reason_code == REASON_SOURCE_IDENTITY_DISAGREEMENT


def test_authorless_page_with_different_checked_isbn_still_conflicts() -> None:
    other = replace(silent(), isbn="9780306406157")
    result = evaluate_grounding_gate(TARGET, [source("named", authors=TARGET.primary_authors), other])
    assert not result.accepted
    assert result.reason_code == REASON_SOURCE_IDENTITY_DISAGREEMENT


def test_two_silent_pages_still_disagree() -> None:
    result = evaluate_grounding_gate(TARGET, [source("one"), silent()])
    assert not result.accepted
    assert result.reason_code == REASON_SOURCE_IDENTITY_DISAGREEMENT


@pytest.mark.parametrize(
    "subject",
    [
        "Robert Harris writes about Brazilian history.",
        "The book is by Robert Harris. It traces Brazilian history.",
        "This book was written by Robert Harris. It traces Brazilian history.",
    ],
)
def test_another_name_in_prose_is_not_an_authorship_claim(subject: str) -> None:
    # Prose attribution is too unreliable to veto (reviewers, forewords); the page is just not used.
    other = replace(source("other"), subject_text=subject)
    named = source("named", authors=TARGET.primary_authors)
    result = evaluate_grounding_gate(TARGET, [named, other])
    assert result.accepted
    assert tuple(item.canonical_url for item in result.accepted_sources) == (named.url,)


@pytest.mark.parametrize(
    "subject",
    [
        "Publishers Weekly wrote that Guedes tells a vivid Brazilian history.",
        "Guedes tells a vivid Brazilian history. Bill Bryson wrote the foreword.",
    ],
)
def test_a_reviewer_or_foreword_name_keeps_target_author_evidence(subject: str) -> None:
    review = replace(source("review"), subject_text=subject)
    named = source("named", authors=TARGET.primary_authors)
    result = evaluate_grounding_gate(TARGET, [named, review])
    assert result.accepted
    assert {item.canonical_url for item in result.accepted_sources} == {named.url, review.url}


@pytest.mark.parametrize(
    "subject",
    [
        "The book discusses Robert Harris and Brazilian history.",
        "Robert Harris appears in this history of Brazil.",
        "The book traces history through Brazilian characters portrayed by their contemporaries.",
        "A history of Brazil shaped by Ancient Traditions.",
        "The book discusses a novel by Robert Harris while tracing Brazilian history.",
    ],
)
def test_merely_mentioning_other_people_does_not_invent_an_author(subject: str) -> None:
    other = replace(source("silent"), subject_text=subject)
    named = source("named", authors=TARGET.primary_authors)
    result = evaluate_grounding_gate(TARGET, [named, other])
    assert result.accepted
    assert tuple(item.canonical_url for item in result.accepted_sources) == (named.url,)


@pytest.mark.parametrize(
    "subject",
    [
        "Octavio Guedes writes about Brazilian history.",
        "The book is by Octavio Guedes. It traces Brazilian history.",
        "This book was written by Octavio Guedes. It traces Brazilian history.",
    ],
)
def test_attributed_target_author_keeps_positive_subject_evidence(subject: str) -> None:
    named = replace(source("named"), subject_text=subject)
    result = evaluate_grounding_gate(TARGET, [named, silent()])
    assert result.accepted
    assert tuple(item.canonical_url for item in result.accepted_sources) == (named.url,)


def test_a_surname_that_is_also_a_title_word_is_not_headline_evidence() -> None:
    target = DurableTarget("smart-mouth", "Smart Mouth", ("Jean Smart",), {})
    page = replace(source("smartypants"), title="Smart Mouth", subject_text="Sweets and snacks for every day.")
    result = evaluate_grounding_gate(target, [page])
    assert not result.accepted
    assert not result.accepted_sources


def test_an_author_name_inside_the_book_title_is_not_headline_evidence() -> None:
    target = DurableTarget("ovitz", "Who Is Michael Ovitz?", ("Michael Ovitz",), {})
    page = replace(source("shop"), title="Who Is Michael Ovitz?", subject_text="A Hollywood memoir.")
    assert not evaluate_grounding_gate(target, [page]).accepted
    named = replace(page, title="Who Is Michael Ovitz? Michael Ovitz looks back on Hollywood")
    assert evaluate_grounding_gate(target, [named]).accepted


def test_a_title_variant_containing_the_author_name_is_not_headline_evidence() -> None:
    target = DurableTarget("diary", "The Diary of Anne Frank", ("Anne Frank",), {})
    page = replace(source("shop"), title="Diary of Anne Frank", subject_text="A wartime diary.")
    assert not evaluate_grounding_gate(target, [page]).accepted
