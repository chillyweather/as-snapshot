"""Offline MVP Pipeline acceptance coverage for the issue #457 closure path."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from typing import ClassVar

from autoscout.grounded_description import GROUNDED_SYNTHESIS_PROMPT
from autoscout.grounded_description import MAX_FETCHES_PER_CANDIDATE
from autoscout.grounded_description import OUTCOME_GROUNDED
from autoscout.grounded_description import STAGE_BUDGET_SECONDS
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import CodexGroundedDescriptionSynthesizer
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import committed_source_classifier
from autoscout.grounded_description import grounding_titles_match
from autoscout.grounded_description import validate_stage_artifact

FIXTURES = Path(__file__).parent / "fixtures" / "issue-457-closure-acceptance"
CHECKED_DATE = "2026-08-17"

GUIDE_BOOKMARKS_URL = (
    "https://bookmarks.reviews/reviews/a-guide-to-open-water-lifesaving-lessons-on-love-care-and-survival-a-memoir/"
)
GUIDE_KIRKUS_URL = "https://www.kirkusreviews.com/book-reviews/virginia-eubanks/a-guide-to-open-water-lifesaving/"
UNSEEN_BOOKMARKS_URL = "https://bookmarks.reviews/reviews/all-thats-unseen-an-appalachian-memoir/"
UNSEEN_PUBLISHER_URL = "https://www.penguinrandomhouse.com/books/748921/all-thats-unseen-by-emilee-hackney/"

# Owners come from the owner policy (#687, #1123): the equivalence table
# gives Book Marks the Literary Hub owner, and every other host here is owned
# by its registrable domain.
EXPECTED_SOURCE_METADATA = {
    GUIDE_BOOKMARKS_URL: {"source_kind": "independent", "source_family": "independent", "owner_id": "literary-hub"},
    GUIDE_KIRKUS_URL: {"source_kind": "independent", "source_family": "independent", "owner_id": "kirkusreviews.com"},
    UNSEEN_BOOKMARKS_URL: {
        "source_kind": "independent",
        "source_family": "independent",
        "owner_id": "literary-hub",
    },
    UNSEEN_PUBLISHER_URL: {
        "source_kind": "independent",
        "source_family": "independent",
        "owner_id": "penguinrandomhouse.com",
    },
}


class FixtureTransport:
    """Serve reduced live captures through the production HTTP transport seam."""

    fixture_names: ClassVar[dict[str, str]] = {
        GUIDE_BOOKMARKS_URL: "bookmarks-guide-to-open-water-lifesaving.html",
        GUIDE_KIRKUS_URL: "kirkus-guide-to-open-water-lifesaving.html",
        UNSEEN_BOOKMARKS_URL: "bookmarks-all-thats-unseen.html",
        UNSEEN_PUBLISHER_URL: "penguin-random-house-all-thats-unseen.html",
    }

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def request(self, url: str, **kwargs: Any) -> HTTPResponse:
        self.calls.append({"url": url, **kwargs})
        fixture_name = self.fixture_names[url]
        return HTTPResponse(
            url=url,
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=(FIXTURES / fixture_name).read_bytes(),
        )


class FixtureDiscovery:
    """Classify exact captured URLs without a search request or network call."""

    urls_by_identity: ClassVar[dict[str, tuple[str, ...]]] = {
        "a guide to open water lifesaving|virginia eubanks": (GUIDE_BOOKMARKS_URL, GUIDE_KIRKUS_URL),
        "all thats unseen|emilee hackney": (UNSEEN_BOOKMARKS_URL, UNSEEN_PUBLISHER_URL),
    }

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.classifier = committed_source_classifier()

    def discover(self, candidate: dict[str, Any], **_kwargs: Any) -> list[DiscoveryResult]:
        identity_key = str(candidate["identity_key"])
        self.calls.append(identity_key)
        results: list[DiscoveryResult] = []
        for url in self.urls_by_identity[identity_key]:
            policy = self.classifier.classify(url)
            results.append(
                DiscoveryResult(
                    url=url,
                    source_kind=str(policy["source_kind"]),
                    source_family=str(policy["source_family"]),
                    owner_id=str(policy["owner_id"]),
                    source_language="en",
                    classification=str(policy["classification"]),
                    classification_evidence=policy,
                )
            )
        return results


class FakeClock:
    def __call__(self) -> float:
        return 0.0


class RecordingSynthesisAgent:
    """Return fixed Russian Content Descriptions and record model-facing input."""

    descriptions: ClassVar[dict[str, str]] = {
        "a guide to open water lifesaving|virginia eubanks": (
            "В мемуаре Вирджиния Юбэнкс рассказывает об уходе за партнёром с "
            "посттравматическим стрессовым расстройством и соединяет личный опыт с "
            "репортажем и инструкцией о поддержке переживших травму. "
            "Редакционный крючок — редкое сочетание исповеди, репортажа и практического руководства."
        ),
        "all thats unseen|emilee hackney": (
            "Эмиль Хакни исследует взросление в Аппалачах, влияние пятидесятнической "
            "церкви, пересечение харизматического христианства, мизогинии и первой любви. "
            "Редакционный крючок — личный взгляд на закрытое сообщество изнутри."
        ),
    }

    def __init__(self) -> None:
        self.calls: list[tuple[list[dict[str, Any]], str | None]] = []

    def generate(self, work_items: list[dict[str, Any]], *, _prompt_override: str | None = None) -> dict[str, Any]:
        self.calls.append((work_items, _prompt_override))
        return {
            str(item["identity_key"]): {
                "content_description": self.descriptions[str(item["identity_key"])],
            }
            for item in work_items
        }


def _candidates() -> list[dict[str, Any]]:
    return [
        {
            "candidate_id": "candidate-guide-to-open-water-lifesaving",
            "identity_key": "a guide to open water lifesaving|virginia eubanks",
            "title": "A Guide to Open Water Lifesaving",
            "primary_authors": ["Virginia Eubanks"],
            "update_delta": "new",
            "first_seen_date": CHECKED_DATE,
            "priority_label": "B",
        },
        {
            "candidate_id": "candidate-all-thats-unseen",
            "identity_key": "all thats unseen|emilee hackney",
            "title": "All That's Unseen",
            "primary_authors": ["Emilee Hackney"],
            "update_delta": "new",
            "first_seen_date": CHECKED_DATE,
            "priority_label": "B",
        },
    ]


def test_mvp_pipeline_scouting_candidates_use_detail_review_evidence_for_grounded_content_descriptions() -> None:
    transport = FixtureTransport()
    discovery = FixtureDiscovery()
    agent = RecordingSynthesisAgent()
    stage = GroundedDescriptionStage(
        discovery=discovery,
        fetch=BoundedHTTPFetcher(transport=transport),
        synthesizer=CodexGroundedDescriptionSynthesizer(agent=agent),
        monotonic=FakeClock(),
    )

    artifact = stage.run(
        _candidates(),
        operation_id="issue-457-closure-acceptance",
        operation_date=CHECKED_DATE,
    )

    assert artifact.stage_budget_seconds == STAGE_BUDGET_SECONDS
    assert stage.max_fetches == MAX_FETCHES_PER_CANDIDATE
    assert artifact.counts == {
        "grounded": 2,
        "single_source": 0,
        "attempted_no_description": 0,
        "not_attempted": 0,
        "evidence_unchanged": 0,
    }
    assert artifact.reason_counts == {}
    assert validate_stage_artifact(artifact)
    assert set(discovery.calls) == {
        "a guide to open water lifesaving|virginia eubanks",
        "all thats unseen|emilee hackney",
    }
    assert {call["url"] for call in transport.calls} == {
        GUIDE_BOOKMARKS_URL,
        GUIDE_KIRKUS_URL,
        UNSEEN_BOOKMARKS_URL,
        UNSEEN_PUBLISHER_URL,
    }
    assert len(transport.calls) == 4
    assert len(agent.calls) == 2
    assert all(prompt == GROUNDED_SYNTHESIS_PROMPT for _items, prompt in agent.calls)

    outcomes = {outcome.identity_key: outcome for outcome in artifact.outcomes}
    guide = outcomes["a guide to open water lifesaving|virginia eubanks"]
    assert guide.outcome == OUTCOME_GROUNDED
    assert tuple(source.canonical_url for source in guide.research_sources) == (
        GUIDE_BOOKMARKS_URL,
        GUIDE_KIRKUS_URL,
    )
    assert guide.to_dict()["research_sources"] == [GUIDE_BOOKMARKS_URL, GUIDE_KIRKUS_URL]
    assert [source.owner_id for source in guide.research_sources] == ["literary-hub", "kirkusreviews.com"]
    assert guide.source_independence_evidence is not None
    assert guide.source_independence_evidence.owner_ids == ("literary-hub", "kirkusreviews.com")
    assert guide.content_description == agent.descriptions[guide.identity_key]

    unseen = outcomes["all thats unseen|emilee hackney"]
    assert unseen.outcome == OUTCOME_GROUNDED
    assert tuple(source.canonical_url for source in unseen.research_sources) == (
        UNSEEN_BOOKMARKS_URL,
        UNSEEN_PUBLISHER_URL,
    )
    assert unseen.to_dict()["research_sources"] == [UNSEEN_BOOKMARKS_URL, UNSEEN_PUBLISHER_URL]
    assert [source.owner_id for source in unseen.research_sources] == ["literary-hub", "penguinrandomhouse.com"]
    assert unseen.source_independence_evidence is not None
    assert unseen.source_independence_evidence.owner_ids == ("literary-hub", "penguinrandomhouse.com")
    assert unseen.content_description == agent.descriptions[unseen.identity_key]

    encoded_by_identity = {str(items[0]["identity_key"]): items[0] for items, _prompt in agent.calls}
    guide_evidence = encoded_by_identity[guide.identity_key]["accepted_bounded_evidence"]
    guide_bookmarks = next(item for item in guide_evidence if item["url"] == GUIDE_BOOKMARKS_URL)
    assert guide_bookmarks["raw_title"] == (
        "Book Marks reviews of A Guide to Open Water Lifesaving: Lessons on Love, Care, and Survival: A Memoir"
    )
    assert guide_bookmarks["raw_authors"] == ["Virginia Eubanks"]
    assert grounding_titles_match("A Guide to Open Water Lifesaving", guide_bookmarks["raw_title"])
    assert "caring for a partner with PTSD" in guide_bookmarks["display_text"]
    assert "overall rating" not in guide_bookmarks["display_text"]
    assert any("partner with PTSD" in item["display_text"] for item in guide_evidence)

    unseen_evidence = encoded_by_identity[unseen.identity_key]["accepted_bounded_evidence"]
    unseen_bookmarks = next(item for item in unseen_evidence if item["url"] == UNSEEN_BOOKMARKS_URL)
    assert unseen_bookmarks["raw_title"] == "Book Marks reviews of All That's Unseen: An Appalachian Memoir"
    assert unseen_bookmarks["raw_authors"] == ["Emilee Hackney"]
    assert grounding_titles_match("All That's Unseen", unseen_bookmarks["raw_title"])
    assert "Pentecostal church" in unseen_bookmarks["display_text"]
    assert "Harvard graduate Hackney debuts" in unseen_bookmarks["display_text"]
    publisher = next(item for item in unseen_evidence if item["url"] == UNSEEN_PUBLISHER_URL)
    assert publisher["raw_title"] == "All That's Unseen"
    assert publisher["raw_authors"] == ["Emilee Hackney"]
    assert "New York Times bestseller" in publisher["display_text"]


def test_bounded_fetcher_keeps_nested_book_marks_subject_text_bounded() -> None:
    class NestedDetailTransport:
        def request(self, url: str, **_kwargs: Any) -> HTTPResponse:
            return HTTPResponse(
                url=url,
                status=200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=(
                    b"<html><head><title>A Guide to Open Water Lifesaving</title>"
                    b"<meta name='author' content='Virginia Eubanks'></head><body>"
                    b"<span class='bookmarks_a_review_pullquote'>Non-div marker text.</span>"
                    b"<div class='bookmarks_a_review_pullquote'>Outer subject."
                    b"<div><span>Nested subject text.</span></div></div>"
                    b"<div>Unrelated trailing page text.</div></body></html>"
                ),
            )

    source = BoundedHTTPFetcher(transport=NestedDetailTransport()).fetch(
        DiscoveryResult(
            url=GUIDE_BOOKMARKS_URL,
            source_kind="review",
            source_family="review_aggregator",
            owner_id="literary-hub",
            source_language="en",
            classification="classified",
        ),
        checked_date=CHECKED_DATE,
    )

    assert "Outer subject." in source.subject_text
    assert "Nested subject text." in source.subject_text
    assert "Non-div marker text." not in source.subject_text
    assert "Unrelated trailing page text." not in source.subject_text


def test_issue_457_closure_identity_and_source_policy_checks_remain_grounded() -> None:
    discovery = FixtureDiscovery()
    for candidate in _candidates():
        results = discovery.discover(candidate)
        assert all(result.classification == "classified" for result in results)
        assert all(result.owner_id for result in results)
        for result in results:
            expected = EXPECTED_SOURCE_METADATA[result.url]
            assert result.source_kind == expected["source_kind"]
            assert result.source_family == expected["source_family"]
            assert result.owner_id == expected["owner_id"]

    for url, expected in EXPECTED_SOURCE_METADATA.items():
        classification = discovery.classifier.classify(url)
        assert classification["classification"] == "classified"
        assert classification["source_kind"] == expected["source_kind"]
        assert classification["source_family"] == expected["source_family"]
        assert classification["owner_id"] == expected["owner_id"]
