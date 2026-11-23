"""Regression coverage for issue #618 fullwidth-colon title matching."""

from __future__ import annotations

import pytest

from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import FetchedSource
from autoscout.grounded_description import evaluate_grounding_gate
from autoscout.grounded_description import grounding_titles_match
from autoscout.identity import normalize_work_title

AUTHOR = "Alex Morgan"
TARGET_TITLE = "情報の未来：石器時代からAIまでの歴史…"


def _source(url: str, title: str, owner_id: str) -> FetchedSource:
    return FetchedSource(
        url=url,
        source_kind="publisher",
        source_family="publisher",
        owner_id=owner_id,
        source_language="en",
        title=title,
        primary_authors=(AUTHOR,),
        subject_text="The book examines automation and human skills in modern work.",
        checked_date="2026-09-01",
    )


def test_truncated_fullwidth_colon_target_matches_sources_with_different_subtitles() -> None:
    target = DurableTarget(
        candidate_id="issue-618-information-future",
        title=TARGET_TITLE,
        primary_authors=(AUTHOR,),
        record={"title": TARGET_TITLE, "primary_authors": [AUTHOR]},
    )
    sources = [
        _source(
            "https://one.example.test/information-future",
            "情報の未来：石器時代からAIまでの完全な歴史",
            "one",
        ),
        _source(
            "https://two.example.test/information-future",
            "情報の未来：石器時代からAIまでの社会史",
            "two",
        ),
    ]

    result = evaluate_grounding_gate(target, sources)

    assert result.accepted
    assert len(result.accepted_sources) == 2
    assert all(item.reason_code != "title_mismatch" for item in result.rejected_sources)


def test_normalize_work_title_ignores_ascii_and_fullwidth_colon_subtitles() -> None:
    assert normalize_work_title("Title: Subtitle") == "title"
    assert normalize_work_title("Title：Subtitle") == "title"


def test_untruncated_fullwidth_colon_title_matches_main_title_only() -> None:
    full_title = "情報の未来：石器時代からAIまでの完全な歴史"

    assert grounding_titles_match(full_title, "情報の未来")


@pytest.mark.parametrize("title", ["Title，Subtitle", "Title　Subtitle"])
def test_non_colon_fullwidth_separators_remain_title_content(title: str) -> None:
    assert normalize_work_title(title) == "title subtitle"
