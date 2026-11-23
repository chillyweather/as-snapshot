"""Regression tests for source-specific non-book and fixture exclusions."""

from __future__ import annotations

import pytest

from autoscout.review_rendering import assemble_cumulative_review
from autoscout.source_eligibility import historical_record_source_eligibility_exclusion_reason
from autoscout.source_eligibility import source_mention_eligibility_exclusion_reason

EXCLUDED = (
    (
        "Chemistry and Our Universe",
        "Ron B. Davis",
        "Audible Bestselling Science & Engineering Audiobooks",
        "non-book material",
    ),
    (
        "How the Earth Works",
        "Michael E. Wysession",
        "Audible Bestselling Science & Engineering Audiobooks",
        "non-book material",
    ),
    (
        "Understanding Artificial Intelligence: Of Minds and Machines",
        "Patrick Grim",
        "Audible Bestselling Computers & Technology Audiobooks",
        "non-book material",
    ),
    (
        "Mafia - zwischen Romantik und Gewalt",
        "BosePark Productions GmbH",
        "Audible.de Bestselling Biografien & Erinnerungen Hoerbuecher",
        "non-book material",
    ),
    (
        "Family Business",
        "Undone Work GmbH",
        "Audible.de Bestselling Biografien & Erinnerungen Hoerbuecher",
        "non-book material",
    ),
    (
        "Les futurs en partage",
        "Élodie Exemple",
        "Nonfiction.fr Critiques",
        "invalid fixture record",
    ),
)


@pytest.mark.parametrize("title,author,source_name,reason", EXCLUDED)
def test_known_non_book_or_fixture_records_are_excluded(title: str, author: str, source_name: str, reason: str) -> None:
    mention = {
        "title": title,
        "primary_authors": [author],
        "source_name": source_name,
        "source_family": "audiobook ranking" if "Audible" in source_name else "media/curated list",
        "source_evidence": [{"source_name": source_name}],
    }

    assert source_mention_eligibility_exclusion_reason(mention) == reason


def test_same_title_from_another_source_is_not_title_wide_banned() -> None:
    mention = {
        "title": "Family Business",
        "primary_authors": ["Undone Work GmbH"],
        "source_name": "Independent Book Review",
        "source_family": "media/curated list",
    }

    assert source_mention_eligibility_exclusion_reason(mention) == ""


def test_current_review_projects_policy_exclusions_out_immediately() -> None:
    candidate = {
        "identity_key": "family business|undone work gmbh",
        "candidate_id": "candidate-family-business",
        "title": "Family Business",
        "primary_authors": ["Undone Work GmbH"],
        "source_evidence": [
            {
                "source_name": "Audible.de Bestselling Biografien & Erinnerungen Hoerbuecher",
                "source_family": "audiobook ranking",
            }
        ],
        "content_description": "",
        "priority_label": "C",
        "independent_signal_count": 1,
    }

    rows, summary = assemble_cumulative_review([candidate], [], [])

    assert rows == []
    assert summary["visible_total_count"] == 0


@pytest.mark.parametrize("title,author,source_name,reason", EXCLUDED)
def test_historical_records_use_the_same_source_specific_policy(
    title: str, author: str, source_name: str, reason: str
) -> None:
    record = {
        "title": title,
        "primary_authors": [author],
        "source_evidence": [
            {
                "source_name": source_name,
                "source_family": "audiobook ranking" if "Audible" in source_name else "media/curated list",
            }
        ],
    }

    assert historical_record_source_eligibility_exclusion_reason(record) == reason
