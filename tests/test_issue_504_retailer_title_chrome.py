"""Offline regression coverage for issue #504 retailer title chrome stripping."""

from __future__ import annotations

from pathlib import Path

from autoscout.grounded_description import REASON_FEWER_THAN_TWO
from autoscout.grounded_description import REASON_SOURCE_IDENTITY_DISAGREEMENT
from autoscout.grounded_description import REASON_TITLE_MISMATCH
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import FetchedSource
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import SourceClassifier
from autoscout.grounded_description import _identity_reason
from autoscout.grounded_description import _source_pair_identity_disagrees
from autoscout.grounded_description import _strip_amazon_listing_chrome
from autoscout.grounded_description import committed_source_classifier
from autoscout.grounded_description import evaluate_grounding_gate
from autoscout.grounded_description import grounding_titles_match

FIXTURES = Path(__file__).parent / "fixtures" / "issue-502-agreeing-subset"
CHECKED_DATE = "2026-08-17"
TARGET_TITLE = "Exhumed: Unearthing the History of the American Vampire"
TARGET_AUTHOR = "Aaron Mahnke"

AMAZON_URL = "https://www.amazon.com/Exhumed-Unearthing-History-American-Vampire/dp/B0G4P2VQ4P"
US_AMAZON_URL = "https://us.amazon.com/Exhumed-Unearthing-History-American-Vampire-ebook/dp/B0GFP7VDW9"
BOOKSHOP_URL = (
    "https://bookshop.org/p/books/exhumed-unearthing-the-history-of-the-american-vampire-aaron-mahnke/c7e72bebb737534a"
)
HACHETTE_URL = "https://www.hachettebookgroup.com/titles/aaron-mahnke/exhumed/9798894141817/"
BARNES_NOBLE_URL = "https://www.barnesandnoble.com/w/exhumed-aaron-mahnke/1148854448"

ALL_URLS = (AMAZON_URL, US_AMAZON_URL, BOOKSHOP_URL, HACHETTE_URL, BARNES_NOBLE_URL)

BODY_FILES: dict[str, str] = {
    AMAZON_URL: "amazon-exhumed.html",
    US_AMAZON_URL: "us-amazon-exhumed-ebook.html",
    BOOKSHOP_URL: "bookshop-exhumed.html",
    HACHETTE_URL: "hachette-exhumed.html",
    BARNES_NOBLE_URL: "barnesandnoble-exhumed.html",
}

EXPECTED_TITLES: dict[str, str] = {
    AMAZON_URL: TARGET_TITLE,
    US_AMAZON_URL: TARGET_TITLE,
    BOOKSHOP_URL: "Exhumed",
    HACHETTE_URL: "Exhumed",
    BARNES_NOBLE_URL: TARGET_TITLE,
}


class FixtureTransport:
    def __init__(self) -> None:
        self.requested_urls: list[str] = []

    def request(self, url: str, **_kwargs: object) -> HTTPResponse:
        self.requested_urls.append(url)
        body = (FIXTURES / BODY_FILES[url]).read_bytes()
        return HTTPResponse(
            url=url,
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=body,
        )


def _classify(classifier: SourceClassifier, url: str) -> dict[str, str]:
    classification = classifier.classify(url)
    assert classification["classification"] == "classified"
    return {
        "source_kind": str(classification["source_kind"]),
        "source_family": str(classification["source_family"]),
        "owner_id": str(classification["owner_id"]),
    }


def _fetched_sources(*urls: str) -> list[FetchedSource]:
    classifier = committed_source_classifier()
    fetcher = BoundedHTTPFetcher(transport=FixtureTransport())
    sources: list[FetchedSource] = []
    for url in urls:
        classification = _classify(classifier, url)
        sources.append(
            fetcher.fetch(
                DiscoveryResult(
                    url=url,
                    source_kind=classification["source_kind"],
                    source_family=classification["source_family"],
                    owner_id=classification["owner_id"],
                    source_language="en",
                    classification="classified",
                ),
                checked_date=CHECKED_DATE,
            )
        )
    return sources


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


def test_issue_504_amazon_listing_title_is_stripped_to_the_book_title() -> None:
    """The faithful Amazon fixture must yield the book title, not listing chrome."""
    amazon = _fetched_sources(AMAZON_URL)[0]

    assert amazon.title == TARGET_TITLE


def test_issue_504_cleaned_title_matches_every_peer_source() -> None:
    """The cleaned Amazon title must match the cleanly-extracted peer titles."""
    amazon = _fetched_sources(AMAZON_URL)[0]
    peers = {
        US_AMAZON_URL: _fetched_sources(US_AMAZON_URL)[0],
        BOOKSHOP_URL: _fetched_sources(BOOKSHOP_URL)[0],
        HACHETTE_URL: _fetched_sources(HACHETTE_URL)[0],
        BARNES_NOBLE_URL: _fetched_sources(BARNES_NOBLE_URL)[0],
    }

    for url, peer in peers.items():
        assert peer.title == EXPECTED_TITLES[url]
        assert grounding_titles_match(amazon.title, peer.title), url


def test_issue_504_cleaned_title_passes_identity_and_pair_agreement() -> None:
    """The cleaned Amazon title must stop being a title mismatch."""
    target = _target()
    amazon, bookshop = _fetched_sources(AMAZON_URL, BOOKSHOP_URL)

    assert _identity_reason(target, amazon) == ""
    assert not _source_pair_identity_disagrees(target, amazon, bookshop)


def test_issue_504_amazon_and_bookshop_pair_grounds_from_agreement() -> None:
    """The agreeing Amazon and Bookshop pair must ground, with no title conflict."""
    result = evaluate_grounding_gate(_target(), _fetched_sources(BOOKSHOP_URL, AMAZON_URL))

    assert result.accepted is True
    assert result.reason_code == ""
    assert tuple(source.canonical_url for source in result.accepted_sources) == (BOOKSHOP_URL,)
    rejected = {item.canonical_url: item.reason_code for item in result.rejected_sources}
    assert rejected == {AMAZON_URL: REASON_FEWER_THAN_TWO}
    assert all(item.reason_code != REASON_TITLE_MISMATCH for item in result.rejected_sources)
    assert all(item.reason_code != REASON_SOURCE_IDENTITY_DISAGREEMENT for item in result.rejected_sources)


def test_issue_504_legitimate_subtitles_survive_on_other_fixtures() -> None:
    """Colon-carrying subtitles on other retailer pages must stay intact."""
    titles = {source.url: source.title for source in _fetched_sources(*ALL_URLS)}

    assert titles == EXPECTED_TITLES


def test_issue_504_french_storefront_label_strips_listing_chrome() -> None:
    """The well-known French storefront label strips the full listing chrome."""
    title = "Amazon.fr: L'Étranger: 9782070360024: Camus, Albert: Livres"

    assert _strip_amazon_listing_chrome(title) == "L'Étranger"


def test_issue_504_german_storefront_label_strips_listing_chrome() -> None:
    """The well-known German storefront label strips the full listing chrome."""
    title = "Amazon.de: Die Verwandlung: 9783596135585: Kafka, Franz: Bücher"

    assert _strip_amazon_listing_chrome(title) == "Die Verwandlung"


def test_issue_504_unlisted_locale_category_is_untouched() -> None:
    """An unlisted locale category keeps the title verbatim."""
    title = "Amazon.co.jp: Some Book: 9781234567890: Doe, Jane: 本"

    assert _strip_amazon_listing_chrome(title) == title


def test_issue_504_chrome_resembling_title_without_isbn_tail_is_untouched() -> None:
    """A real title with only a retailer-like prefix must not be truncated."""
    title = "Amazon.com: Get Big Fast"

    assert _strip_amazon_listing_chrome(title) == title


def test_issue_504_isbn_tail_without_retailer_prefix_is_untouched() -> None:
    """A trailing ISBN shape without the retailer prefix must stay intact."""
    title = "Exhumed: 9798894141817: Mahnke, Aaron: Books"

    assert _strip_amazon_listing_chrome(title) == title


def test_issue_504_all_chrome_shape_without_title_segment_is_untouched() -> None:
    """Chrome with no remaining title segment must be left verbatim."""
    title = "Amazon.com: 9798894141817: Mahnke, Aaron: Books"

    assert _strip_amazon_listing_chrome(title) == title


def test_issue_504_unrecognized_category_segment_is_untouched() -> None:
    """A tail whose category segment is not a known book category must survive."""
    title = "Amazon.com: Exhumed: 9798894141817: Mahnke, Aaron: Electronics"

    assert _strip_amazon_listing_chrome(title) == title


def test_issue_504_asin_segment_is_not_treated_as_isbn() -> None:
    """An ASIN-looking segment is not an ISBN anchor, so nothing is stripped."""
    title = "Amazon.com: Exhumed: B0G4P2VQ4P: Mahnke, Aaron: Books"

    assert _strip_amazon_listing_chrome(title) == title


def test_issue_504_nested_real_title_keeps_its_own_amazon_prefix() -> None:
    """One chrome layer is removed while a real title keeps its own prefix."""
    title = "Amazon.com: Amazon.com: Get Big Fast: 0066620814: Spector, Robert: Books"

    assert _strip_amazon_listing_chrome(title) == "Amazon.com: Get Big Fast"


def test_issue_523_kindle_ebook_chrome_is_stripped_to_book_title() -> None:
    """The Kindle eBook listing chrome must be stripped to the book title."""
    title = "Amazon.com: You Do You: 100+ Endlessly Adaptable Recipes to Boost Your Culinary Confidence eBook : Wolf, Serena: Kindle Store"

    assert (
        _strip_amazon_listing_chrome(title)
        == "You Do You: 100+ Endlessly Adaptable Recipes to Boost Your Culinary Confidence"
    )


def test_issue_523_kindle_category_without_ebook_marker_is_untouched() -> None:
    """A known Kindle category without the eBook title marker is not stripped."""
    title = "Amazon.com: You Do You: 100+ Endlessly Adaptable Recipes to Boost Your Culinary Confidence : Wolf, Serena: Kindle Store"

    assert _strip_amazon_listing_chrome(title) == title


def test_issue_523_real_title_resembling_ebook_shape_without_anchor_is_untouched() -> None:
    """A title resembling the eBook shape but missing the retailer prefix is untouched."""
    title = "You Do You: 100+ Endlessly Adaptable Recipes to Boost Your Culinary Confidence eBook : Wolf, Serena: Kindle Store"

    assert _strip_amazon_listing_chrome(title) == title


def test_issue_523_genuine_ebook_title_with_subtitle_and_chrome_is_stripped() -> None:
    """A real title containing eBook: plus chrome eBook marker strips only the chrome."""
    title = "Amazon.com: How to Write an eBook: A Guide eBook : Doe, Jane: Kindle Store"

    assert _strip_amazon_listing_chrome(title) == "How to Write an eBook: A Guide"


def test_issue_523_short_titles_ending_with_stopword_still_strip() -> None:
    """Short real titles ending with a stopword must still strip the Kindle chrome."""
    assert _strip_amazon_listing_chrome("Amazon.com: Just Do It eBook : Doe, Jane: Kindle Store") == "Just Do It"
    assert _strip_amazon_listing_chrome("Amazon.com: Own It eBook : Doe, Jane: Kindle Store") == "Own It"


def test_issue_523_title_colon_without_a_space_is_not_the_chrome_marker() -> None:
    """The space before the colon is the signal, so "eBook:" in a real title is not chrome."""
    title = "Amazon.com: Writing an eBook: Doe, Jane: Kindle Store"

    assert _strip_amazon_listing_chrome(title) == title


def test_issue_504_unrelated_title_passes_through() -> None:
    assert _strip_amazon_listing_chrome("Salt: A World History") == "Salt: A World History"


def _synthetic_amazon_source(page_title: str) -> FetchedSource:
    body = (
        f"<html><head><title>{page_title}</title>"
        '<meta name="description" content="A book about something.">'
        "</head><body><p>A book about something.</p></body></html>"
    ).encode()

    class SyntheticTransport:
        def request(self, url: str, **_kwargs: object) -> HTTPResponse:
            return HTTPResponse(
                url=url,
                status=200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=body,
            )

    classifier = committed_source_classifier()
    classification = _classify(classifier, AMAZON_URL)
    fetcher = BoundedHTTPFetcher(transport=SyntheticTransport())
    return fetcher.fetch(
        DiscoveryResult(
            url=AMAZON_URL,
            source_kind=classification["source_kind"],
            source_family=classification["source_family"],
            owner_id=classification["owner_id"],
            source_language="en",
            classification="classified",
        ),
        checked_date=CHECKED_DATE,
    )


def test_issue_504_synthetic_amazon_page_fetch_strips_chrome_before_cleaning() -> None:
    """A delimiter inside the book-title segment does not hide the listing tail."""
    source = _synthetic_amazon_source("Amazon.com: Some Book - Full Title: 9781234567890: Doe, Jane: Books")

    assert source.title == "Some Book"


def test_issue_504_synthetic_amazon_page_fetch_retries_cleaned_title() -> None:
    """A suffix delimiter after the chrome shape is handled by the cleaned fallback."""
    source = _synthetic_amazon_source("Amazon.com: Some Book: 9781234567890: Doe, Jane: Books :: Amazon.com")

    assert source.title == "Some Book"


def test_issue_504_synthetic_amazon_page_fetch_preserves_chrome_resembling_title() -> None:
    """Real Amazon captures are bot-gated here, so this negative case uses a synthetic page."""
    source = _synthetic_amazon_source("Amazon.com: Get Big Fast")

    assert source.title == "Amazon.com: Get Big Fast"


def test_issue_504_schema_org_name_with_headline_by_is_stored_whole() -> None:
    """A schema.org name containing " by " must be stored whole, never split like a headline."""
    cases = [
        ("Death by Black Hole: And Other Cosmic Quandaries", "Neil deGrasse Tyson"),
        ("Sleeping by the Mississippi", "Wim Wenders"),
        ("Exhumed: Unearthing the History of the American Vampire", "Aaron Mahnke"),
    ]

    for title, author in cases:
        body = (
            f"<html><head><title>{title} - Bookshop</title></head><body>"
            '<script type="application/ld+json">'
            f'{{"@context": "https://schema.org", "@type": "Book", "name": "{title}", '
            f'"author": {{"@type": "Person", "name": "{author}"}}}}'
            "</script></body></html>"
        ).encode()

        class SyntheticTransport:
            def request(self, url: str, _body: bytes = body, **_kwargs: object) -> HTTPResponse:
                return HTTPResponse(
                    url=url,
                    status=200,
                    headers={"content-type": "text/html; charset=utf-8"},
                    body=_body,
                )

        classifier = committed_source_classifier()
        classification = _classify(classifier, BOOKSHOP_URL)
        fetcher = BoundedHTTPFetcher(transport=SyntheticTransport())
        source = fetcher.fetch(
            DiscoveryResult(
                url=BOOKSHOP_URL,
                source_kind=classification["source_kind"],
                source_family=classification["source_family"],
                owner_id=classification["owner_id"],
                source_language="en",
                classification="classified",
            ),
            checked_date=CHECKED_DATE,
        )

        assert source.title == title
