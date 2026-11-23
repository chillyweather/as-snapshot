"""Regression coverage for issue #756: several authors printed in one value.

The Bokhandlerforeningen list prints its whole author cell as
``Hevrøy, Hans Ola / Ngan, John``, and the ranking contract keeps that cell
verbatim. The description stage read it as one person, so no page could match
it and the search asked for a phrase no page contains.
"""

from __future__ import annotations

import pytest

from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import FetchedSource
from autoscout.grounded_description import _author_keys
from autoscout.grounded_description import _author_subject_terms
from autoscout.grounded_description import _build_discovery_queries
from autoscout.grounded_description import _identity_reason
from autoscout.grounded_description import _source_has_target_author_evidence

# Author cells of the 2026-10-05 Bokhandlerforeningen capture, as printed.
INVERTED_PAIR = "Hevrøy, Hans Ola / Ngan, John"
DISPLAY_PAIR = "Ragnar Hovland / Steffen Kverneland"
INVERTED_TRIPLE = "Mashadi, Vita / Mashadi, Wanda / Jenssen, Tonje"
TITLE = "Evig fysen"


def _target(*authors: str) -> DurableTarget:
    return DurableTarget(
        candidate_id="issue-756-target",
        title=TITLE,
        primary_authors=authors,
        record={"title": TITLE, "primary_authors": list(authors)},
    )


def _source(*, authors: tuple[str, ...] = (), subject_text: str = "") -> FetchedSource:
    return FetchedSource(
        url="https://bokhandel.example.test/evig-fysen",
        source_kind="publisher",
        source_family="publisher",
        owner_id="publisher-supplied-copy",
        source_language="no",
        title=TITLE,
        primary_authors=authors,
        subject_text=subject_text,
        checked_date="2026-10-05",
    )


@pytest.mark.parametrize(
    ("value", "people"),
    [
        (INVERTED_PAIR, {"hans ola hevrøy", "john ngan"}),
        (DISPLAY_PAIR, {"ragnar hovland", "steffen kverneland"}),
        (INVERTED_TRIPLE, {"vita mashadi", "wanda mashadi", "tonje jenssen"}),
    ],
)
def test_a_spaced_slash_separates_people(value: str, people: set[str]) -> None:
    assert _author_keys([value]) == people


def test_an_ampersand_still_separates_people() -> None:
    assert _author_keys(["Øystein Olsen & Eivind Thomassen"]) == {"øystein olsen", "eivind thomassen"}


@pytest.mark.parametrize("name", ["AC/DC", "Hans/Ola Hevrøy", "Hevrøy, Hans Ola"])
def test_a_single_name_stays_one_person(name: str) -> None:
    """Only a slash with whitespace on both sides joins two people."""
    assert len(_author_keys([name])) == 1


def test_a_page_naming_one_of_the_listed_authors_matches() -> None:
    source = _source(authors=("John Ngan",))

    assert _source_has_target_author_evidence(_target(INVERTED_PAIR), source)
    assert _identity_reason(_target(INVERTED_PAIR), source) == ""


def test_a_page_naming_another_author_still_conflicts() -> None:
    source = _source(authors=("Kari Nordmann",))

    assert not _source_has_target_author_evidence(_target(INVERTED_PAIR), source)
    assert _identity_reason(_target(INVERTED_PAIR), source) == "author_mismatch"


def test_subject_text_naming_a_listed_author_is_author_evidence() -> None:
    source = _source(subject_text="Hans Ola Hevrøy skriver om mat og vekt i denne boken.")

    assert _source_has_target_author_evidence(_target(INVERTED_PAIR), source)


def test_each_listed_author_contributes_its_own_surname_term() -> None:
    assert {"hevrøy", "ngan"} <= _author_subject_terms([INVERTED_PAIR])


def test_the_search_names_each_author_in_reading_order() -> None:
    candidate = {
        "title": "For mine sønner",
        "primary_authors": ["Iraki, Juma / Oksvold, Ronny"],
        "isbn": "9788202896485",
    }

    assert _build_discovery_queries(candidate) == ['"For mine sønner" Juma Iraki, Ronny Oksvold book 9788202896485']


def test_the_search_turns_one_inverted_author_into_reading_order() -> None:
    candidate = {"title": "Babymatboka", "primary_authors": ["Stuhaug, Linda"]}

    assert _build_discovery_queries(candidate) == ['"Babymatboka" Linda Stuhaug book']


def test_the_search_leaves_a_plain_author_as_printed() -> None:
    candidate = {"title": "Astrup", "primary_authors": ["Ragnar Hovland", "Steffen Kverneland"]}

    assert _build_discovery_queries(candidate) == ['"Astrup" Ragnar Hovland, Steffen Kverneland book']


def test_the_search_names_a_repeated_author_once() -> None:
    candidate = {"title": "Astrup", "primary_authors": ["Hovland, Ragnar", "Ragnar Hovland / Steffen Kverneland"]}

    assert _build_discovery_queries(candidate) == ['"Astrup" Ragnar Hovland, Steffen Kverneland book']


def test_the_stored_author_values_are_not_rewritten() -> None:
    candidate = {"title": TITLE, "primary_authors": [INVERTED_PAIR]}

    _build_discovery_queries(candidate)

    assert candidate["primary_authors"] == [INVERTED_PAIR]
