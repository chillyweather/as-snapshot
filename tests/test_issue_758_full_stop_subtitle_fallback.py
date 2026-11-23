"""Regression coverage for issue #758: a subtitle printed after a full stop.

Nonfiction.fr prints ``Main title. Subtitle``. The discovery query quoted the
whole string, the search returned nothing, and the main-title fallback of
issue #708 never ran because it knew only the colon and the spaced dash.
"""

from __future__ import annotations

import pytest

from autoscout.grounded_description import _build_discovery_queries

# Titles of the 2026-10-05 Nonfiction.fr mentions that ended without a description.
ANIMAUX = "L’Année terrible des animaux. Un regard inédit sur le Siège et la Commune de Paris. 1870-1871"
OFFICER = "Chief Geopolitical Officer. Agir en entrepreneur, penser en géopoliticien"


def _queries(title: str, author: str = "Nicolas Baron") -> list[str]:
    return _build_discovery_queries({"title": title, "primary_authors": [author]})


def test_a_full_stop_subtitle_yields_the_main_title_query() -> None:
    assert _queries(ANIMAUX) == [
        f'"{ANIMAUX}" Nicolas Baron book',
        '"L’Année terrible des animaux" Nicolas Baron book',
    ]


def test_the_main_title_ends_at_the_first_subtitle_full_stop() -> None:
    assert _queries(OFFICER, "Florent Parmentier")[1] == '"Chief Geopolitical Officer" Florent Parmentier book'


def test_a_colon_still_wins_over_a_later_full_stop() -> None:
    assert _queries("Main Title: A Subtitle. And More")[1] == '"Main Title" Nicolas Baron book'


@pytest.mark.parametrize(
    "title",
    [
        "Dr. Seuss Goes to War",  # one word before the full stop
        "St. Louis Blues",
        "Life with Mr. Darcy",  # an abbreviation ends the candidate main title
        "Paris vs. London",
        "Histoire de France. tome premier",  # no capital after the full stop
        "Version 2.0 of Everything",  # no space after the full stop
        "The End.",  # nothing after the full stop
    ],
)
def test_an_abbreviation_full_stop_adds_no_query(title: str) -> None:
    assert len(_queries(title)) == 1


def test_a_truncated_title_still_adds_no_query() -> None:
    assert len(_queries("L’Année terrible des animaux. Un regard…")) == 1
