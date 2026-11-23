"""Regression coverage for issue #757: page-title shapes that hid the book title.

Replaying the rejected pages of the 2026-10-05 run found two shapes. A
tronsmo.no product page ends its title with ``TRONSMO`` after an en dash,
which was not removed as a site suffix. An erkeengler.no review is headed
``Juma Iraki: For mine sønner``, and the work key kept the author's name in
front of the colon.
"""

from __future__ import annotations

import pytest

from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import FetchedSource
from autoscout.grounded_description import _clean_page_title
from autoscout.grounded_description import _discovery_relevance_tier
from autoscout.grounded_description import _identity_reason
from autoscout.grounded_description import _source_pair_identity_disagrees

IRAKI_AUTHORS = "Iraki, Juma / Oksvold, Ronny"


def _target(title: str, *authors: str) -> DurableTarget:
    return DurableTarget(
        candidate_id="issue-757-target",
        title=title,
        primary_authors=authors,
        record={"title": title, "primary_authors": list(authors)},
    )


def _source(
    title: str, *, url: str = "https://erkeengler.example.test/review", authors: tuple[str, ...] = ()
) -> FetchedSource:
    return FetchedSource(
        url=url,
        source_kind="review",
        source_family="review",
        owner_id=url,
        source_language="no",
        title=title,
        primary_authors=authors,
        subject_text="Juma Iraki skriver til sønnene sine om krig, flukt og et nytt liv i Norge.",
        checked_date="2026-10-05",
    )


@pytest.mark.parametrize("dash", ["-", "–", "—"])
def test_a_spaced_dash_ends_the_title_before_a_site_suffix(dash: str) -> None:
    assert _clean_page_title(f"Bakmennene {dash} TRONSMO") == "Bakmennene"


def test_a_one_word_title_behind_an_en_dash_suffix_matches_its_candidate() -> None:
    page = _source(_clean_page_title("Bakmennene – TRONSMO"), url="https://www.tronsmo.no/produkt/bakmennene/")

    assert _identity_reason(_target("Bakmennene", "Kari Nordmann"), page) == ""


def test_an_unspaced_dash_inside_a_title_is_kept() -> None:
    assert _clean_page_title("Grense–land") == "Grense–land"


def test_a_title_with_a_spaced_en_dash_keeps_only_its_first_part() -> None:
    """The defined behaviour for a real title that contains a spaced en dash.

    Like a spaced hyphen, the dash ends the title. A first part of two or more
    words still matches the full candidate title through phrase containment;
    a one-word first part does not, under the unchanged two-word rule.
    """
    assert _clean_page_title("Kristin Lavransdatter – Kransen") == "Kristin Lavransdatter"
    assert (
        _identity_reason(
            _target("Kristin Lavransdatter – Kransen", "Sigrid Undset"),
            _source(_clean_page_title("Kristin Lavransdatter – Kransen")),
        )
        == ""
    )
    assert (
        _identity_reason(
            _target("Salt – en roman", "Kari Nordmann"),
            _source(_clean_page_title("Salt – en roman")),
        )
        == "title_mismatch"
    )


def test_an_author_headline_matches_the_candidate_after_the_colon() -> None:
    page = _source("Juma Iraki: For mine sønner")

    assert _identity_reason(_target("For mine sønner", IRAKI_AUTHORS), page) == ""


def test_an_inverted_author_headline_also_matches() -> None:
    page = _source("Iraki, Juma: For mine sønner")

    assert _identity_reason(_target("For mine sønner", IRAKI_AUTHORS), page) == ""


def test_a_colon_title_whose_first_part_is_not_a_target_author_is_unchanged() -> None:
    page = _source("Kari Nordmann: For mine sønner")

    assert _identity_reason(_target("For mine sønner", IRAKI_AUTHORS), page) == "title_mismatch"


def test_a_subtitle_colon_still_keeps_the_main_title() -> None:
    page = _source("For mine sønner: brev fra en far")

    assert _identity_reason(_target("For mine sønner", IRAKI_AUTHORS), page) == ""


def test_an_author_headline_agrees_with_a_plain_title_page() -> None:
    target = _target("For mine sønner", IRAKI_AUTHORS)
    headline = _source("Juma Iraki: For mine sønner", authors=("Juma Iraki",))
    plain = _source("For mine sønner", url="https://forlag.example.test/bok", authors=("Juma Iraki",))

    assert not _source_pair_identity_disagrees(target, headline, plain)


def test_an_author_headline_discovery_result_is_ranked_first() -> None:
    target = {"title": "For mine sønner", "primary_authors": [IRAKI_AUTHORS]}
    result = {
        "url": "https://erkeengler.no/blog/2026/08/05/juma-iraki-for-mine-sonner/",
        "title": "Juma Iraki: For mine sønner",
        "route_kind": "article",
    }

    assert _discovery_relevance_tier(target, result) == 0


def test_a_corporate_author_opening_the_title_still_matches_the_full_title() -> None:
    """``H Mart`` is both a listed author and part of the candidate title."""
    target = _target("Cooking with H Mart: The Essential Guide", "Stacey Kwon", "H Mart")

    assert _identity_reason(target, _source("H Mart: The Cookbook")) == ""
