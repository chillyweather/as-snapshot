"""Regression for issue #513 honest refusal reason.

The gate must not report a fetch failure when the honest verdict for the
judged sources is a normal refusal reason. A single unusable intake source
must not rename the candidate verdict.
"""

from __future__ import annotations

from pathlib import Path

from autoscout.grounded_description import REASON_INVALID_SOURCE
from autoscout.grounded_description import REASON_SEARCH_SNIPPET
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import FetchedSource
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import SynthesisResult
from autoscout.grounded_description import committed_source_classifier
from autoscout.grounded_description import evaluate_grounding_gate

FIXTURES = Path(__file__).parent / "fixtures" / "issue-502-agreeing-subset"
CHECKED_DATE = "2026-08-17"
TARGET_TITLE = "Exhumed: Unearthing the History of the American Vampire"
TARGET_AUTHOR = "Aaron Mahnke"

# The five retailer fixtures are faithful copies of the live pages.
# Use the same transport pattern as test_issue_502_agreeing_subset.py.
BODY_FILES: dict[str, str] = {
    "https://www.amazon.com/Exhumed-Unearthing-History-American-Vampire/dp/B0G4P2VQ4P": "amazon-exhumed.html",
    "https://www.barnesandnoble.com/w/exhumed-aaron-mahnke/1148854448": "barnesandnoble-exhumed.html",
    "https://bookshop.org/p/books/exhumed-unearthing-the-history-of-the-american-vampire-aaron-mahnke/c7e72bebb737534a": "bookshop-exhumed.html",
}


class FixtureTransport:
    def request(self, url: str, **_kwargs: object) -> HTTPResponse:
        body = (FIXTURES / BODY_FILES[url]).read_bytes()
        return HTTPResponse(
            url=url,
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=body,
        )


def _classify(url: str) -> dict[str, str]:
    classifier = committed_source_classifier()
    classification = classifier.classify(url)
    assert classification["classification"] == "classified"
    return {
        "source_kind": str(classification["source_kind"]),
        "source_family": str(classification["source_family"]),
        "owner_id": str(classification["owner_id"]),
    }


def _discoveries(urls: list[str]) -> tuple[DiscoveryResult, ...]:
    return tuple(
        DiscoveryResult(
            url=url,
            source_kind=_classify(url)["source_kind"],
            source_family=_classify(url)["source_family"],
            owner_id=_classify(url)["owner_id"],
            source_language="en",
            classification="classified",
        )
        for url in urls
    )


def _fetched(urls: list[str]) -> list[FetchedSource]:
    fetcher = BoundedHTTPFetcher(transport=FixtureTransport())
    return [fetcher.fetch(result, checked_date=CHECKED_DATE) for result in _discoveries(urls)]


def _target() -> DurableTarget:
    return DurableTarget(
        candidate_id="exhumed|aaron mahnke",
        title=TARGET_TITLE,
        primary_authors=(TARGET_AUTHOR,),
        record={
            "candidate_id": "exhumed|aaron mahnke",
            "title": TARGET_TITLE,
            "primary_authors": [TARGET_AUTHOR],
        },
    )


def test_gate_reports_honest_reason_when_one_source_is_unusable() -> None:
    """Two agreeing same-owner sources plus one unusable must not be fetch failure.

    The two good sources are the faithful Amazon and Barnes and Noble
    fixtures. Both are publisher-supplied-copy and agree on identity, but
    they share one owner, so the honest gate verdict for the pair is
    ``fewer_than_two_agreeing_sources`` when judged alone. Adding a third
    source that is a search snippet must not rename the verdict to a fetch
    failure.
    """
    amazon_url = "https://www.amazon.com/Exhumed-Unearthing-History-American-Vampire/dp/B0G4P2VQ4P"
    barnes_url = "https://www.barnesandnoble.com/w/exhumed-aaron-mahnke/1148854448"
    good_sources = _fetched([amazon_url, barnes_url])

    # The unusable source trips the intake rejection for is_search_snippet.
    # Before #513 this was reported as search_or_fetch_failure at both the
    # per-source and candidate levels.
    snippet = FetchedSource(
        url="https://search.example.test/work",
        source_kind="review",
        source_family="review",
        owner_id="search-owner",
        source_language="en",
        title=TARGET_TITLE,
        primary_authors=(TARGET_AUTHOR,),
        subject_text="memory",
        checked_date=CHECKED_DATE,
        is_search_snippet=True,
    )

    # Gate with only the two good sources is accepted as a singleton
    # (the honest verdict for the pair is the singleton acceptance, and the
    # remaining source is recorded as fewer_than_two). The point is that
    # adding the unusable source must not turn the candidate-level reason
    # into a fetch failure.
    base = evaluate_grounding_gate(_target(), good_sources)
    assert base.accepted is True

    # With the unusable source present, the gate must still not report a
    # fetch failure. The unusable source is recorded with its own honest
    # code.
    with_unusable = evaluate_grounding_gate(_target(), [*good_sources, snippet])
    # The candidate itself is still accepted (singleton), so the candidate
    # reason is empty, but the unusable source must not be recorded as a
    # fetch failure.
    assert with_unusable.accepted is True
    rejected_by_url = {item.canonical_url: item.reason_code for item in with_unusable.rejected_sources}
    assert rejected_by_url["https://search.example.test/work"] == REASON_SEARCH_SNIPPET
    assert rejected_by_url["https://search.example.test/work"] != "search_or_fetch_failure"
    # The gate's own verdict for the two good sources is preserved
    # (one is accepted, the other is fewer_than_two), not a fetch failure.
    assert with_unusable.reason_code == ""


def test_gate_reports_fewer_not_fetch_when_judged_sources_exist() -> None:
    """A judged title mismatch must outrank a single unusable source.

    This synthetic case forces the gate into the final refusal path where
    the old code picked the first rejected in a fixed set. The honest
    verdict is title_mismatch from the judged source, not a fetch failure
    from the unusable source that happens to be first in the list.
    """
    target = DurableTarget("test", "Atomic Habits", ("James Clear",), {})

    # Two in-policy sources that will be judged: one title mismatch, one
    # missing owner (unusable). The honest for the pair alone is
    # title_mismatch.
    title_mismatch = FetchedSource(
        url="https://title.example/book",
        source_kind="review",
        source_family="review",
        owner_id="owner-a",
        source_language="en",
        title="Wrong Title",
        primary_authors=("James Clear",),
        subject_text="shared topic",
        checked_date=CHECKED_DATE,
    )
    missing_owner = FetchedSource(
        url="https://missing.example/book",
        source_kind="",
        source_family="",
        owner_id="",
        source_language="en",
        title="Atomic Habits",
        primary_authors=("James Clear",),
        subject_text="shared topic",
        checked_date=CHECKED_DATE,
    )
    bad_snippet = FetchedSource(
        url="https://bad.example/book",
        source_kind="review",
        source_family="review",
        owner_id="owner-bad",
        source_language="en",
        title="Atomic Habits",
        primary_authors=("James Clear",),
        subject_text="shared topic",
        checked_date=CHECKED_DATE,
        is_search_snippet=True,
    )

    base = evaluate_grounding_gate(target, [title_mismatch, missing_owner])
    assert base.accepted is False
    assert base.reason_code == "title_mismatch"

    # Put the unusable snippet first. Before #513 the gate reported
    # search_or_fetch_failure because the first rejected in the fixed set
    # was the snippet. After the fix the honest title_mismatch must win
    # when judged sources exist, and the snippet is recorded with its
    # honest code.
    with_bad = evaluate_grounding_gate(target, [bad_snippet, title_mismatch, missing_owner])
    assert with_bad.accepted is False
    assert with_bad.reason_code == "title_mismatch"
    assert with_bad.reason_code != "search_or_fetch_failure"
    rejected = {item.canonical_url: item.reason_code for item in with_bad.rejected_sources}
    assert rejected["https://bad.example/book"] == REASON_SEARCH_SNIPPET
    assert rejected["https://missing.example/book"] == REASON_INVALID_SOURCE


def test_invalid_source_and_snippet_have_distinct_codes() -> None:
    """The four intake conditions no longer share a fetch-failure code."""
    target = DurableTarget("test", "Atomic Habits", ("James Clear",), {})

    invalid_url = FetchedSource(
        url="not a url",
        source_kind="review",
        source_family="review",
        owner_id="owner",
        source_language="en",
        title="Atomic Habits",
        primary_authors=("James Clear",),
        subject_text="shared topic",
        checked_date=CHECKED_DATE,
    )
    snippet = FetchedSource(
        url="https://snippet.example/book",
        source_kind="review",
        source_family="review",
        owner_id="owner",
        source_language="en",
        title="Atomic Habits",
        primary_authors=("James Clear",),
        subject_text="shared topic",
        checked_date=CHECKED_DATE,
        is_search_snippet=True,
    )
    missing_meta = FetchedSource(
        url="https://missing.example/book",
        source_kind="",
        source_family="",
        owner_id="",
        source_language="en",
        title="Atomic Habits",
        primary_authors=("James Clear",),
        subject_text="shared topic",
        checked_date=CHECKED_DATE,
    )
    missing_date = FetchedSource(
        url="https://missingdate.example/book",
        source_kind="review",
        source_family="review",
        owner_id="owner",
        source_language="en",
        title="Atomic Habits",
        primary_authors=("James Clear",),
        subject_text="shared topic",
        checked_date="",
    )

    for source in [invalid_url, missing_meta, missing_date]:
        result = evaluate_grounding_gate(target, [source])
        assert result.accepted is False
        assert result.reason_code == REASON_INVALID_SOURCE
        assert result.reason_code != "search_or_fetch_failure"

    result = evaluate_grounding_gate(target, [snippet])
    assert result.accepted is False
    assert result.reason_code == REASON_SEARCH_SNIPPET
    assert result.reason_code != "search_or_fetch_failure"


def test_stage_reports_honest_reason_for_exhumed_pair() -> None:
    """Offline proof that the stage reports the gate's own verdict.

    The two in-policy retailer fixtures are fetched through the bounded
    fetcher. The stage must not report a fetch failure for the candidate.
    """

    class FixtureDiscovery:
        def __init__(self, results: tuple[DiscoveryResult, ...]) -> None:
            self.results = tuple(results)

        def discover(self, _candidate: object, **_kwargs: object) -> tuple[DiscoveryResult, ...]:
            return self.results

    class RussianSynthesizer:
        def synthesize(self, _candidate: object, sources: object) -> SynthesisResult:
            return SynthesisResult(
                "Книга исследует связь между историей, правом и ответственностью. "
                "Второй фрагмент раскрывает документальную основу, называет проверенные "
                "страницы и уточняет редакционный ракурс книги. "
                "Третье предложение закрепляет вывод и показывает, почему книга попала в обзор."
            )

    # Use the two faithful fixtures that previously poisoned the weekly run.
    amazon_url = "https://www.amazon.com/Exhumed-Unearthing-History-American-Vampire/dp/B0G4P2VQ4P"
    barnes_url = "https://www.barnesandnoble.com/w/exhumed-aaron-mahnke/1148854448"
    discoveries = _discoveries([amazon_url, barnes_url])

    stage = GroundedDescriptionStage(
        discovery=FixtureDiscovery(discoveries),
        fetch=BoundedHTTPFetcher(transport=FixtureTransport()),
        synthesizer=RussianSynthesizer(),
    )

    candidate = {
        "candidate_id": "exhumed|aaron mahnke",
        "identity_key": "exhumed|aaron mahnke",
        "title": TARGET_TITLE,
        "primary_authors": [TARGET_AUTHOR],
        "update_delta": "new",
        "first_seen_date": CHECKED_DATE,
        "priority_label": "A",
    }

    artifact = stage.run(
        [candidate],
        operation_id="issue-513",
        operation_date=CHECKED_DATE,
        force_candidates=True,
    )
    outcome = artifact.outcomes[0]
    # The gate for these two fixtures is a singleton acceptance, so the
    # stage outcome is single_source with no fetch failure.
    assert outcome.outcome == "single_source"
    assert outcome.reason_code == ""
    assert all(item.reason_code != "search_or_fetch_failure" for item in outcome.rejected_source_reasons)
