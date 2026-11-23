"""Offline regression coverage for issue #508 candidate-relevance ordering.

The stage must prioritise candidate-specific product pages (exact work title
plus a target-author token in the bounded discovery title) within the
three-fetch budget, and must persist deterministic ordering reasons so the
persisted artifact validates against the same shared ordering rule.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import ClassVar

from autoscout.grounded_description import OUTCOME_ATTEMPTED_NO_DESCRIPTION
from autoscout.grounded_description import OUTCOME_GROUNDED
from autoscout.grounded_description import OUTCOME_SINGLE_SOURCE
from autoscout.grounded_description import REASON_SOURCE_IDENTITY_DISAGREEMENT
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import GroundedDescriptionStageService
from autoscout.grounded_description import HTTPDiscoveryProvider
from autoscout.grounded_description import SynthesisResult
from autoscout.grounded_description import committed_source_classifier
from autoscout.grounded_description import validate_stage_artifact

# The shared tier/key functions are imported inside the tests that use them:
# before the implementation lands they do not exist yet, and the stage-seam
# tests must fail on fetch order, not on module import.

CHECKED_DATE = "2026-09-07"
TARGET_TITLE = "You Can Do Beautiful Things"
TARGET_AUTHOR = "Nana Kwame Adjei-Brenyah"
PRH_URL = "https://www.penguinrandomhouse.com/books/748921/you-can-do-beautiful-things/"
PRH_TITLE = f"{TARGET_TITLE} by {TARGET_AUTHOR} | Penguin Random House"
AMAZON_URLS = (
    "https://www.amazon.com/gp/bestsellers/books",
    "https://www.amazon.com/gp/bestsellers/books/1",
    "https://www.amazon.com/gp/bestsellers/books/2",
)
DP_URL = "https://www.amazon.com/dp/0593831403"


def _candidate(
    *,
    candidate_id: str = "you-can-do-beautiful-things",
    identity_key: str = "you can do beautiful things|nana kwame adjei-brenyah",
    title: str = TARGET_TITLE,
    primary_authors: Sequence[str] = (TARGET_AUTHOR,),
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "identity_key": identity_key,
        "title": title,
        "primary_authors": list(primary_authors),
        "first_seen_date": CHECKED_DATE,
        "priority_label": "A",
        "update_delta": "new",
    }


def _discovery(
    url: str,
    *,
    title: str = "",
    route_kind: str = "",
    source_kind: str = "publisher",
    source_family: str = "publisher_copy",
    owner_id: str = "",
) -> dict[str, Any]:
    return {
        "url": url,
        "source_kind": source_kind,
        "source_family": source_family,
        "owner_id": owner_id or f"owner-{url}",
        "source_language": "en",
        "classification": "classified",
        "title": title,
        "route_kind": route_kind,
    }


def _fetched_source(
    url: str,
    *,
    owner: str | None = None,
    authorless: bool = False,
    title: str = TARGET_TITLE,
    primary_authors: Sequence[str] = (TARGET_AUTHOR,),
    subject_text: str = "A novel about beauty, family, and the power of art.",
    subject_key: str = "beauty family art",
    checked_date: str = CHECKED_DATE,
    classification: str = "classified",
) -> dict[str, Any]:
    return {
        "url": url,
        "source_kind": "publisher",
        "source_family": "publisher_copy",
        "owner_id": owner or f"owner-{url}",
        "source_language": "en",
        "title": title,
        "primary_authors": [] if authorless else list(primary_authors),
        "subject_text": subject_text,
        "subject_key": subject_key,
        "checked_date": checked_date,
        "classification": classification,
    }


class DeterministicDiscovery:
    def __init__(self, results: Sequence[Mapping[str, Any] | DiscoveryResult]) -> None:
        self.results = tuple(results)

    def discover(self, _candidate: Mapping[str, Any], **_kwargs: Any) -> Sequence[Mapping[str, Any] | DiscoveryResult]:
        return self.results


class RecordingFetch:
    def __init__(
        self,
        *,
        failing: set[str] | None = None,
        authorless: bool = False,
        fetched_title: str = TARGET_TITLE,
        fetched_authors: Sequence[str] = (TARGET_AUTHOR,),
        evidence_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self.requested_urls: list[str] = []
        self.failing = set(failing or ())
        self.authorless = authorless
        self.fetched_title = fetched_title
        self.fetched_authors = tuple(fetched_authors)
        self.evidence_overrides = {url: dict(values) for url, values in (evidence_overrides or {}).items()}

    def fetch(self, result: Mapping[str, Any] | DiscoveryResult, **_kwargs: Any) -> dict[str, Any]:
        url = result.url if isinstance(result, DiscoveryResult) else str(result.get("url", ""))
        self.requested_urls.append(url)
        if url in self.failing:
            raise RuntimeError("injected fetch failure")
        overrides = self.evidence_overrides.get(url, {})
        return _fetched_source(
            url,
            authorless=self.authorless,
            title=str(overrides.get("title", self.fetched_title)),
            primary_authors=overrides.get("primary_authors", self.fetched_authors),
            subject_text=str(overrides.get("subject_text", "A novel about beauty, family, and the power of art.")),
            subject_key=str(overrides.get("subject_key", "beauty family art")),
            checked_date=str(overrides.get("checked_date", CHECKED_DATE)),
            classification=str(overrides.get("classification", "classified")),
        )


class DeterministicSynthesizer:
    def synthesize(self, _candidate: Mapping[str, Any], sources: Sequence[object]) -> SynthesisResult:
        return SynthesisResult(
            "Книга о красоте, семье и силе искусства. "
            "Второй фрагмент раскрывает документальную основу, называет проверенные "
            "страницы и уточняет редакционный ракурс книги. "
            "Третье предложение закрепляет вывод и показывает, почему книга попала в обзор."
        )


def _build_stage(
    discoveries: Sequence[Mapping[str, Any] | DiscoveryResult],
    *,
    fetch: RecordingFetch,
) -> GroundedDescriptionStage:
    return GroundedDescriptionStage(
        discovery=DeterministicDiscovery(discoveries),
        fetch=fetch,
        synthesizer=DeterministicSynthesizer(),
    )


def _run_stage(
    discoveries: Sequence[Mapping[str, Any] | DiscoveryResult],
    *,
    fetch: RecordingFetch | None = None,
    candidate: Mapping[str, Any] | None = None,
) -> Any:
    return _build_stage(discoveries, fetch=fetch or RecordingFetch()).run(
        [_candidate() if candidate is None else candidate],
        operation_id="issue-508",
        operation_date=CHECKED_DATE,
        force_candidates=True,
    )


def _run_service(discoveries: Sequence[Mapping[str, Any] | DiscoveryResult], *, fetch: RecordingFetch) -> Any:
    return GroundedDescriptionStageService(_build_stage(discoveries, fetch=fetch)).run(
        [_candidate()],
        operation_id="issue-508-service",
        operation_date=CHECKED_DATE,
        force_candidates=True,
    )


def _assert_described_outcome(artifact: Any) -> None:
    outcomes = artifact.outcomes
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.outcome in {OUTCOME_SINGLE_SOURCE, OUTCOME_GROUNDED}
    assert outcome.content_description
    assert validate_stage_artifact(artifact.to_dict())


def _prh_scenario() -> tuple[tuple[Mapping[str, Any] | DiscoveryResult, ...], RecordingFetch]:
    fetch = RecordingFetch()
    discoveries = (
        _discovery(AMAZON_URLS[0], title="Amazon Best Sellers", route_kind="bestseller"),
        _discovery(AMAZON_URLS[1], title="Amazon Best Sellers - Books", route_kind="bestseller"),
        _discovery(AMAZON_URLS[2], title="Amazon Best Sellers - Fiction", route_kind="bestseller"),
        _discovery(PRH_URL, title=PRH_TITLE, route_kind="product"),
    )
    return discoveries, fetch


def test_a_exact_publisher_page_is_fetched_before_generic_amazon_pages() -> None:
    discoveries, fetch = _prh_scenario()
    artifact = _run_stage(discoveries, fetch=fetch)

    outcome = artifact.outcomes[0]
    assert fetch.requested_urls[0] == PRH_URL
    assert PRH_URL in [source.canonical_url for source in outcome.research_sources]
    assert len(fetch.requested_urls) <= 3
    _assert_described_outcome(artifact)


def test_b_product_route_outranks_bestseller_and_loses_to_tier_zero() -> None:
    fetch = RecordingFetch()
    artifact = _run_stage(
        [
            _discovery(AMAZON_URLS[0], title="Amazon Best Sellers", route_kind="bestseller"),
            _discovery(DP_URL, title=TARGET_TITLE, route_kind="product"),
            _discovery(PRH_URL, title=PRH_TITLE, route_kind="product"),
        ],
        fetch=fetch,
    )

    assert fetch.requested_urls == [PRH_URL, DP_URL, AMAZON_URLS[0]]
    _assert_described_outcome(artifact)


def test_c_ordinal_tie_break_beats_lexical_canonical_order() -> None:
    z_url = "https://retail.example.test/zzz-result"
    a_url = "https://retail.example.test/aaa-result"
    fetch = RecordingFetch()
    artifact = _run_stage([_discovery(z_url), _discovery(a_url)], fetch=fetch)

    assert fetch.requested_urls == [z_url, a_url]
    assert artifact.outcomes[0].outcome == OUTCOME_GROUNDED
    assert validate_stage_artifact(artifact.to_dict())


def test_d_fetch_failure_continues_to_the_next_ordered_result() -> None:
    bestseller = _discovery(AMAZON_URLS[0], title="Amazon Best Sellers", route_kind="bestseller")
    second_bestseller = _discovery(AMAZON_URLS[1], title="Amazon Best Sellers - Books", route_kind="bestseller")
    fetch = RecordingFetch(failing={PRH_URL})
    artifact = _run_stage(
        [
            bestseller,
            _discovery(PRH_URL, title=PRH_TITLE, route_kind="product"),
            second_bestseller,
        ],
        fetch=fetch,
    )

    outcome = artifact.outcomes[0]
    assert fetch.requested_urls == [PRH_URL, AMAZON_URLS[0], AMAZON_URLS[1]]
    assert outcome.outcome == OUTCOME_GROUNDED


def test_e_bounded_count_six_eligible_results_three_fetches() -> None:
    discoveries = [_discovery(f"https://retail.example.test/work-{index}") for index in range(6)]
    fetch = RecordingFetch()
    artifact = _run_stage(discoveries, fetch=fetch)

    assert len(fetch.requested_urls) == 3
    assert artifact.outcomes[0].outcome in {OUTCOME_SINGLE_SOURCE, OUTCOME_GROUNDED}


def test_f_tier_zero_discovery_does_not_accept_an_authorless_fetched_page() -> None:
    fetch = RecordingFetch(authorless=True)
    artifact = _run_stage(
        [
            _discovery(AMAZON_URLS[0], title="Amazon Best Sellers", route_kind="bestseller"),
            _discovery(AMAZON_URLS[1], title="Amazon Best Sellers - Books", route_kind="bestseller"),
            _discovery(PRH_URL, title=PRH_TITLE, route_kind="product"),
        ],
        fetch=fetch,
    )

    outcome = artifact.outcomes[0]
    assert fetch.requested_urls[0] == PRH_URL
    assert outcome.outcome == OUTCOME_ATTEMPTED_NO_DESCRIPTION
    assert not outcome.content_description
    assert outcome.reason_code == REASON_SOURCE_IDENTITY_DISAGREEMENT


def test_g_search_snippet_is_dropped_before_ordering_and_never_becomes_evidence() -> None:
    snippet = DiscoveryResult(
        url="https://snippet.example.test/result",
        source_kind="publisher",
        source_family="publisher_copy",
        owner_id="owner-snippet",
        source_language="en",
        classification="classified",
        is_search_snippet=True,
        title="snippet title",
    )
    good = _discovery("https://retail.example.test/work")
    fetch = RecordingFetch()
    artifact = _run_stage([snippet, good], fetch=fetch)

    outcome = artifact.outcomes[0]
    assert fetch.requested_urls == [good["url"]]
    assert outcome.outcome == OUTCOME_SINGLE_SOURCE
    assert snippet.url not in [source.canonical_url for source in outcome.research_sources]
    records = outcome.to_dict()["research_source_records"]
    good_records = [record for record in records if record["url"] == good["url"]]
    assert len(good_records) == 1
    assert good_records[0]["discovery_ordinal"] == 1


def test_h_persisted_ordering_reasons_validate_and_legacy_artifacts_still_validate() -> None:
    discoveries, fetch = _prh_scenario()
    artifact = _run_stage(discoveries, fetch=fetch)
    outcome = artifact.outcomes[0]
    records = outcome.to_dict()["research_source_records"]
    by_url = {record["url"]: record for record in records}

    prh_record = by_url[PRH_URL]
    assert prh_record["relevance_tier"] == 0
    assert prh_record["discovery_ordinal"] == 3
    assert prh_record["discovery_title"] == PRH_TITLE
    amazon_record = by_url[AMAZON_URLS[0]]
    assert amazon_record["relevance_tier"] == 2
    assert amazon_record["discovery_ordinal"] == 0
    assert amazon_record["discovery_title"] == "Amazon Best Sellers"
    assert validate_stage_artifact(artifact.to_dict())
    round_tripped = type(artifact).from_mapping(artifact.to_dict()).to_dict()
    assert round_tripped["outcomes"][0]["research_source_records"] == records

    tampered = json.loads(json.dumps(artifact.to_dict()))
    tampered_records = tampered["outcomes"][0]["research_source_records"]
    ordinal_targets = [record for record in tampered_records if record["url"] == AMAZON_URLS[0]]
    assert len(ordinal_targets) == 1
    ordinal_targets[0]["discovery_ordinal"] = 99
    assert not validate_stage_artifact(tampered)

    legacy = json.loads(json.dumps(artifact.to_dict()))
    legacy_records = legacy["outcomes"][0]["research_source_records"]
    stripped = [
        {
            key: value
            for key, value in record.items()
            if key not in {"relevance_tier", "discovery_ordinal", "discovery_title"}
        }
        for record in legacy_records
    ]
    ordered = sorted(stripped, key=lambda record: record["url"])
    legacy["outcomes"][0]["research_source_records"] = ordered
    legacy["outcomes"][0]["research_sources"] = [record["url"] for record in ordered]
    legacy["outcomes"][0]["description_provenance"]["evidence_basis"] = [record["url"] for record in ordered]
    independence = legacy["outcomes"][0]["source_independence_evidence"]
    independence["canonical_urls"] = [record["url"] for record in ordered]
    independence["owner_ids"] = [record["owner_id"] for record in ordered]
    independence["source_families"] = [record["source_family"] for record in ordered]
    assert validate_stage_artifact(legacy)


def test_i_shared_tier_and_ordering_key_unit_semantics() -> None:
    from autoscout.grounded_description import _discovery_ordering_key
    from autoscout.grounded_description import _discovery_relevance_tier

    target = DurableTarget(
        candidate_id="you-can-do-beautiful-things",
        title=TARGET_TITLE,
        primary_authors=(TARGET_AUTHOR,),
        record={
            "candidate_id": "you-can-do-beautiful-things",
            "title": TARGET_TITLE,
            "primary_authors": [TARGET_AUTHOR],
        },
    )

    assert _discovery_relevance_tier(target, _discovery(PRH_URL, title=PRH_TITLE, route_kind="product")) == 0
    assert _discovery_relevance_tier(target, _discovery(DP_URL, title=TARGET_TITLE, route_kind="product")) == 1
    assert _discovery_relevance_tier(target, _discovery(DP_URL, title="Retailer chrome", route_kind="product")) == 1
    assert _discovery_relevance_tier(target, _discovery(DP_URL, title=PRH_TITLE, route_kind="product")) == 0
    assert _discovery_relevance_tier(target, _discovery(PRH_URL, title=PRH_TITLE, route_kind="")) == 0
    for route in ("bestseller", "category", "new_release", "author", "catalog", "search"):
        assert _discovery_relevance_tier(target, _discovery(AMAZON_URLS[0], title=PRH_TITLE, route_kind=route)) == 2

    key = _discovery_ordering_key
    assert key(0, "publisher", 5, "https://z.example.test/") < key(2, "publisher", 0, "https://a.example.test/")
    assert key(2, "review", 0, "https://z.example.test/") < key(2, "publisher", 0, "https://a.example.test/")
    assert key(2, "publisher", 0, "https://z.example.test/") < key(2, "publisher", 1, "https://a.example.test/")
    assert key(2, "publisher", 0, "https://a.example.test/") == key(2, "publisher", 0, "https://a.example.test/")


class _FakeTransport:
    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.results = results
        self.requested_url = ""

    def request(self, url: str, **_kwargs: Any) -> Any:
        self.requested_url = url

        class _Body:
            status = 200
            headers: ClassVar[dict[str, str]] = {}
            body = json.dumps({"results": self.results}).encode("utf-8")

        return _Body()


def test_j_provider_bounds_the_discovery_title() -> None:
    from autoscout.grounded_description import MAX_DISCOVERY_TITLE_TEXT

    long_title = "T" * 400
    transport = _FakeTransport(
        [
            {"url": DP_URL, "title": long_title},
            {"url": AMAZON_URLS[0], "title": "Best Sellers"},
        ]
    )
    provider = HTTPDiscoveryProvider(
        "http://127.0.0.1:8899/search?q={query}",
        transport=transport,
        classifier=committed_source_classifier(),
    )

    results = provider.discover({"title": "A Book", "primary_authors": ["Alex Author"]})
    by_url = {result.url: result for result in results}

    dp_result = by_url[DP_URL]
    assert dp_result.title == long_title[:MAX_DISCOVERY_TITLE_TEXT]
    assert len(dp_result.title) == MAX_DISCOVERY_TITLE_TEXT


def test_l_service_seam_uses_the_same_corrected_fetch_order() -> None:
    discoveries, fetch = _prh_scenario()
    artifact = _run_service(discoveries, fetch=fetch)

    outcome = artifact.outcomes[0]
    assert fetch.requested_urls[0] == PRH_URL
    assert PRH_URL in [source.canonical_url for source in outcome.research_sources]
    _assert_described_outcome(artifact)


REVIEW_URL = "https://www.kirkusreviews.com/book-reviews/nana-kwame-adjei-brenyah/you-can-do-beautiful-things/"
SEARCH_URL = "https://www.amazon.com/s?k=you-can-do-beautiful-things"


def test_m_candidate_specific_review_page_outranks_product_and_generic_routes() -> None:
    fetch = RecordingFetch()
    artifact = _run_stage(
        [
            _discovery(AMAZON_URLS[0], title="Amazon Best Sellers", route_kind="bestseller"),
            _discovery(DP_URL, title=TARGET_TITLE, route_kind="product"),
            _discovery(
                REVIEW_URL,
                title=PRH_TITLE,
                route_kind="",
                source_kind="review",
                source_family="trade_review",
            ),
        ],
        fetch=fetch,
    )

    assert fetch.requested_urls == [REVIEW_URL, DP_URL, AMAZON_URLS[0]]
    _assert_described_outcome(artifact)


def test_n_search_route_chrome_title_never_enters_tier_zero() -> None:
    fetch = RecordingFetch()
    artifact = _run_stage(
        [
            _discovery(SEARCH_URL, title=PRH_TITLE, route_kind="search"),
            _discovery(DP_URL, title=TARGET_TITLE, route_kind="product"),
        ],
        fetch=fetch,
    )

    assert fetch.requested_urls[0] == DP_URL
    assert fetch.requested_urls[0] != SEARCH_URL
    assert SEARCH_URL not in fetch.requested_urls[:1]
    _assert_described_outcome(artifact)


def test_p_title_prefixed_product_route_outranks_bestseller() -> None:
    product_url = "https://www.amazon.com/Some-Book-Title/dp/0593831403"
    fetch = RecordingFetch()
    artifact = _run_stage(
        [
            _discovery(AMAZON_URLS[0], title="Amazon Best Sellers", route_kind="bestseller"),
            _discovery(product_url, title=TARGET_TITLE, route_kind="product"),
        ],
        fetch=fetch,
    )

    assert fetch.requested_urls[0] == product_url
    _assert_described_outcome(artifact)


def test_q_duplicate_urls_keep_the_raw_provider_ordinal() -> None:
    good = _discovery("https://retail.example.test/work")
    duplicate = _discovery(good["url"])
    second = _discovery("https://retail.example.test/next")
    fetch = RecordingFetch()
    artifact = _run_stage([good, duplicate, second], fetch=fetch)

    outcome = artifact.outcomes[0]
    assert fetch.requested_urls == [good["url"], second["url"]]
    records = {record["url"]: record for record in outcome.to_dict()["research_source_records"]}
    assert records[good["url"]]["discovery_ordinal"] == 0
    assert records[second["url"]]["discovery_ordinal"] == 2


def test_r_galey_alix_exact_prh_page_precedes_amazon_bestseller_pages() -> None:
    candidate = _candidate(
        candidate_id="candidate-e8220a41360340b1c7bf31ab",
        identity_key="you can do beautiful things|galey alix",
        title="You Can Do Beautiful Things",
        primary_authors=["Galey Alix"],
    )
    prh_url = "https://www.penguinrandomhouse.com/books/748922/you-can-do-beautiful-things/"
    fetch = RecordingFetch(fetched_title="You Can Do Beautiful Things", fetched_authors=["Galey Alix"])
    artifact = _run_stage(
        [
            _discovery(
                "https://www.amazon.com/gp/bestsellers/books", title="Amazon Best Sellers", route_kind="bestseller"
            ),
            _discovery(
                "https://www.amazon.com/gp/bestsellers/books/1",
                title="Amazon Best Sellers - Books",
                route_kind="bestseller",
            ),
            _discovery(
                "https://www.amazon.com/gp/bestsellers/books/2",
                title="Amazon Best Sellers - Fiction",
                route_kind="bestseller",
            ),
            _discovery(
                prh_url,
                title="You Can Do Beautiful Things by Galey Alix | Penguin Random House",
                route_kind="product",
            ),
        ],
        fetch=fetch,
        candidate=candidate,
    )

    outcome = artifact.outcomes[0]
    assert fetch.requested_urls[0] == prh_url
    assert len(fetch.requested_urls) <= 3
    assert prh_url in [source.canonical_url for source in outcome.research_sources]
    _assert_described_outcome(artifact)


def test_s_serena_wolf_same_author_wrong_work_does_not_outrank_exact_page() -> None:
    candidate = _candidate(
        candidate_id="candidate-b2a913fe3aeac2d0ce35b73a",
        identity_key="you do you|serena wolf",
        title="You Do You",
        primary_authors=["Serena Wolf"],
    )
    dude_diet_url = "https://www.penguinrandomhouse.com/books/643107/the-dude-diet/"
    exact_url = "https://www.penguinrandomhouse.com/books/643108/you-do-you/"
    fetch = RecordingFetch(
        fetched_title="You Do You",
        fetched_authors=["Serena Wolf"],
        evidence_overrides={dude_diet_url: {"title": "The Dude Diet"}},
    )
    artifact = _run_stage(
        [
            _discovery(
                dude_diet_url,
                title="The Dude Diet by Serena Wolf | Penguin Random House",
                route_kind="product",
            ),
            _discovery(
                "https://www.amazon.com/gp/bestsellers/books/serena-wolf",
                title="Amazon Best Sellers - Books",
                route_kind="bestseller",
            ),
            _discovery(
                exact_url,
                title="You Do You by Serena Wolf | Penguin Random House",
                route_kind="product",
            ),
        ],
        fetch=fetch,
        candidate=candidate,
    )

    outcome = artifact.outcomes[0]
    accepted_urls = [source.canonical_url for source in outcome.research_sources]
    assert fetch.requested_urls[0] == exact_url
    assert fetch.requested_urls.index(exact_url) < fetch.requested_urls.index(dude_diet_url)
    assert exact_url in accepted_urls
    assert dude_diet_url not in accepted_urls
    _assert_described_outcome(artifact)


def test_t_exact_publisher_and_retailer_pages_fill_the_fetch_budget() -> None:
    candidate = _candidate(
        candidate_id="candidate-04046edae2f93cf72d3e3904",
        identity_key="your fellow americans|peter santenello",
        title="Your Fellow Americans",
        primary_authors=["Peter Santenello"],
    )
    simon_url = "https://www.simonandschuster.com/books/Your-Fellow-Americans/Peter-Santenello/Hardcover"
    barnes_url = "https://www.barnesandnoble.com/w/your-fellow-americans-peter-santenello/1148674519"
    bookshop_url = "https://bookshop.org/p/books/your-fellow-americans-peter-santenello/1234567"
    fetch = RecordingFetch(fetched_title="Your Fellow Americans", fetched_authors=["Peter Santenello"])
    artifact = _run_stage(
        [
            _discovery(
                "https://www.amazon.com/gp/bestsellers/books",
                title="Amazon Best Sellers",
                route_kind="bestseller",
            ),
            _discovery(
                "https://www.amazon.com/gp/browse.html?node=17",
                title="Amazon Books Category",
                route_kind="category",
            ),
            _discovery(
                "https://www.amazon.com/gp/bestsellers/books/fiction",
                title="Amazon Fiction Best Sellers",
                route_kind="bestseller",
            ),
            _discovery(
                simon_url,
                title="Your Fellow Americans by Peter Santenello | Simon & Schuster",
                route_kind="product",
                source_kind="publisher",
                source_family="publisher_copy",
            ),
            _discovery(
                barnes_url,
                title="Your Fellow Americans by Peter Santenello | Barnes & Noble",
                route_kind="product",
                source_kind="publisher",
                source_family="retail_listing",
            ),
            _discovery(
                bookshop_url,
                title="Your Fellow Americans by Peter Santenello | Bookshop.org",
                route_kind="product",
                source_kind="publisher",
                source_family="retail_listing",
            ),
        ],
        fetch=fetch,
        candidate=candidate,
    )

    assert fetch.requested_urls == [simon_url, barnes_url, bookshop_url]
    assert all(
        url not in fetch.requested_urls
        for url in (
            "https://www.amazon.com/gp/bestsellers/books",
            "https://www.amazon.com/gp/browse.html?node=17",
            "https://www.amazon.com/gp/bestsellers/books/fiction",
        )
    )
    _assert_described_outcome(artifact)


def test_u_exact_simonschuster_page_precedes_weak_amazon_evidence() -> None:
    candidate = _candidate(
        candidate_id="candidate-2a27d18034366434d6efac76",
        identity_key="more please|rachel mansfield",
        title="More, Please!",
        primary_authors=["Rachel Mansfield"],
    )
    simon_url = "https://www.simonandschuster.com/books/More-Please/Rachel-Mansfield/"
    fetch = RecordingFetch(fetched_title="More, Please!", fetched_authors=["Rachel Mansfield"])
    artifact = _run_stage(
        [
            _discovery(
                "https://www.amazon.com/gp/bestsellers/books/rachel-mansfield",
                title="Amazon Best Sellers",
                route_kind="bestseller",
            ),
            _discovery(
                "https://www.amazon.com/gp/browse.html?node=23",
                title="Amazon Books Category",
                route_kind="category",
            ),
            _discovery(
                simon_url,
                title="More, Please! by Rachel Mansfield | Simon & Schuster",
                route_kind="product",
            ),
        ],
        fetch=fetch,
        candidate=candidate,
    )

    outcome = artifact.outcomes[0]
    assert fetch.requested_urls[0] == simon_url
    assert simon_url in [source.canonical_url for source in outcome.research_sources]
    _assert_described_outcome(artifact)
