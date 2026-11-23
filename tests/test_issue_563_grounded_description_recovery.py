"""Offline regression coverage for the 2026-08-22 description fallbacks (#563).

Four books kept the standard fallback after a live weekly run. Three causes
were found and each one is covered here:

* a work title that contains " by " was split into a false author,
* a retailer bot check was accepted as an evidence-free source, and
* the only authoritative pages for the books were outside the source policy.
"""

from __future__ import annotations

import pytest

from autoscout.grounded_description import REASON_PAGE_CHALLENGE_NO_EVIDENCE
from autoscout.grounded_description import BoundedHTTPError
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import HostRateLimiter
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import _strip_amazon_listing_chrome
from autoscout.grounded_description import _title_and_headline_author
from autoscout.grounded_description import _title_is_site_label
from autoscout.grounded_description import committed_source_classifier
from autoscout.grounded_description import grounding_titles_match

CHECKED_DATE = "2026-08-23"

# The observed Barnes & Noble page title for "The 25th Hour". The subtitle
# ends in " by Getting Out of Your Own Way", which is the shape that produced
# the false author.
BY_IN_TITLE = "The 25th Hour: How to Thrive (And Gain Ridiculous Amounts of Time) by Getting Out of Your Own Way"

PAGE_URL = "https://www.barnesandnoble.com/w/the-25th-hour-neal-schore/1148559905"

# The observed bot check answered on an Amazon product URL, so the challenge
# cases use that host: the site-name title is what marks the page as chrome.
CHALLENGE_URL = "https://www.amazon.com/25th-Hour-Ridiculous-Amounts-Getting/dp/1668090120"

REAL_PAGE = (
    f"<html><head><title>{BY_IN_TITLE}</title>"
    '<meta property="og:description" content="Schore shows how a working week can hold more '
    'hours than it appears to hold, and how ordinary teams reclaim them.">'
    "</head><body><p>Body text.</p></body></html>"
)

# A retailer bot check: status 200, a site-name title, no book record, no
# author, and no subject text.
CHALLENGE_PAGE = "<html><head><title>Amazon.com</title></head><body></body></html>"

# A page that names the work but carries nothing else is still evidence of
# identity, so it must survive.
TITLE_ONLY_PAGE = f"<html><head><title>{BY_IN_TITLE}</title></head><body></body></html>"


class ScriptedTransport:
    """Answer each request with the next scripted body."""

    def __init__(self, *bodies: str) -> None:
        self._bodies = list(bodies)
        self.requested_urls: list[str] = []

    def request(self, url: str, **_kwargs: object) -> HTTPResponse:
        self.requested_urls.append(url)
        body = self._bodies[min(len(self.requested_urls) - 1, len(self._bodies) - 1)]
        return HTTPResponse(
            url=url,
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=body.encode("utf-8"),
        )


def _discovery(url: str = PAGE_URL) -> DiscoveryResult:
    return DiscoveryResult(
        url=url,
        source_kind="publisher",
        source_family="retail_listing",
        owner_id="publisher-supplied-copy",
        source_language="en",
    )


def _fetcher(transport: ScriptedTransport, **kwargs: object) -> BoundedHTTPFetcher:
    return BoundedHTTPFetcher(transport=transport, min_host_interval_seconds=0.0, **kwargs)


# ── A work title that contains " by " ────────────────────────────────────


def test_title_containing_by_keeps_the_whole_title_and_claims_no_author() -> None:
    assert _title_and_headline_author(BY_IN_TITLE) == (BY_IN_TITLE, "")


def test_real_byline_after_a_by_inside_the_title_is_still_read() -> None:
    title, author = _title_and_headline_author(f"{BY_IN_TITLE} by Neal Schore")

    assert title == BY_IN_TITLE
    assert author == "Neal Schore"


def test_ordinary_byline_still_splits() -> None:
    assert _title_and_headline_author("Baldwin: A Love Story by Nicholas Boggs") == (
        "Baldwin: A Love Story",
        "Nicholas Boggs",
    )


def test_byline_with_a_trailing_site_label_still_splits() -> None:
    assert _title_and_headline_author("Some Book by J. R. R. Tolkien | Barnes & Noble") == (
        "Some Book",
        "J. R. R. Tolkien",
    )


def test_fetched_page_with_by_in_the_title_carries_no_invented_author() -> None:
    fetched = _fetcher(ScriptedTransport(REAL_PAGE)).fetch(_discovery(), checked_date=CHECKED_DATE)

    assert fetched.title == BY_IN_TITLE
    assert fetched.primary_authors == ()


# ── A retailer bot check ─────────────────────────────────────────────────


def test_evidence_free_page_is_refused_and_retried() -> None:
    transport = ScriptedTransport(CHALLENGE_PAGE)
    fetcher = _fetcher(transport)

    with pytest.raises(BoundedHTTPError) as error:
        fetcher.fetch(_discovery(CHALLENGE_URL), checked_date=CHECKED_DATE)

    assert error.value.reason_code == REASON_PAGE_CHALLENGE_NO_EVIDENCE
    assert len(transport.requested_urls) == fetcher.challenge_attempts


def test_retry_after_a_bot_check_returns_the_real_page() -> None:
    transport = ScriptedTransport(CHALLENGE_PAGE, REAL_PAGE)

    fetched = _fetcher(transport).fetch(_discovery(CHALLENGE_URL), checked_date=CHECKED_DATE)

    assert len(transport.requested_urls) == 2
    assert fetched.title == BY_IN_TITLE
    assert fetched.subject_text.startswith("Schore shows how")


def test_page_that_names_only_the_work_is_still_returned() -> None:
    fetched = _fetcher(ScriptedTransport(TITLE_ONLY_PAGE)).fetch(
        _discovery(CHALLENGE_URL),
        checked_date=CHECKED_DATE,
    )

    assert fetched.title == BY_IN_TITLE
    assert fetched.subject_text == ""


# ── Per-host spacing ─────────────────────────────────────────────────────


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_host_rate_limiter_spaces_repeat_requests_to_one_host() -> None:
    clock = FakeClock()
    limiter = HostRateLimiter(1.0, monotonic=clock.monotonic, sleeper=clock.sleep)

    assert limiter.wait("www.amazon.com") == 0.0
    assert limiter.wait("www.amazon.com") == 1.0
    assert clock.slept == [1.0]


def test_host_rate_limiter_does_not_delay_a_different_host() -> None:
    clock = FakeClock()
    limiter = HostRateLimiter(1.0, monotonic=clock.monotonic, sleeper=clock.sleep)

    limiter.wait("www.amazon.com")

    assert limiter.wait("www.christianbook.com") == 0.0
    assert clock.slept == []


def test_fetcher_spaces_repeat_requests_to_one_host() -> None:
    clock = FakeClock()
    transport = ScriptedTransport(REAL_PAGE)
    fetcher = BoundedHTTPFetcher(
        transport=transport,
        min_host_interval_seconds=1.0,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )

    fetcher.fetch(_discovery(), checked_date=CHECKED_DATE)
    fetcher.fetch(_discovery(), checked_date=CHECKED_DATE)

    assert clock.slept == [1.0]


# ── Owner policy coverage (path rules retired in #1123) ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    (
        "https://www.christianbook.com/imagine-seeing-what-sees-living-loved/9780785294689/pd/294689",
        "https://www.audible.com/pd/The-25th-Hour-Audiobook/B0G2KX7XYS",
    ),
)
def test_storefronts_from_the_failed_run_share_the_publisher_copy_owner(url: str) -> None:
    classifier = committed_source_classifier()

    classification = classifier.classify(url)

    assert classification["classification"] == "classified"
    assert classification["owner_id"] == "publisher-supplied-copy"


# ── Amazon listing chrome at the end of the title ────────────────────────


AMAZON_TRAILING_CHROME = (
    "More Than You Can Imagine: What God Does When You Trust Him: "
    "Larson, Susie, Lee and Leslie Strobel: 9780785294689: Amazon.com: Books"
)


def test_trailing_amazon_chrome_is_stripped_to_the_book_title() -> None:
    assert (
        _strip_amazon_listing_chrome(AMAZON_TRAILING_CHROME)
        == "More Than You Can Imagine: What God Does When You Trust Him"
    )


def test_trailing_chrome_title_agrees_with_a_peer_source() -> None:
    """The failure this repairs: chrome kept in one title split the pair."""
    peer = "More Than You Can Imagine: What God Does When You Trust Him"

    assert grounding_titles_match(_strip_amazon_listing_chrome(AMAZON_TRAILING_CHROME), peer)


@pytest.mark.parametrize(
    "title",
    (
        # No retailer domain segment.
        "Some Book: Author, A: 9781234567897: Example.com: Books",
        # No ISBN segment.
        "Some Book: Author, A: not-an-isbn: Amazon.com: Books",
        # A real title that merely ends in a category word.
        "A Real Title: About Books",
        # The leading layout must keep working.
        "Amazon.com: Get Big Fast",
    ),
)
def test_titles_without_every_trailing_anchor_are_untouched(title: str) -> None:
    assert _strip_amazon_listing_chrome(title) == title


def test_regional_storefront_bot_check_is_recognized() -> None:
    """The bot check titles itself "Amazon.com" even on us.amazon.com."""
    assert _title_is_site_label("Amazon.com", hostname="us.amazon.com")
    assert not _title_is_site_label("Holy Health", hostname="us.amazon.com")


# ── The reason column after a repair ─────────────────────────────────────


def test_repaired_row_drops_the_reason_of_the_attempt_that_failed_before_it() -> None:
    """A grounded row must not keep the previous attempt's refusal reason."""
    from autoscout.review_rendering import _render_neutral_row

    candidate = {
        "identity_key": "the 25th hour|neal schore",
        "candidate_id": "candidate-001",
        "title": "The 25th Hour",
        "primary_authors": ["Neal Schore"],
    }
    stale_row = {
        "Title": "The 25th Hour",
        "Author": "Neal Schore",
        "Description reason": "Автор источника не совпадает",
    }
    grounded_stage_record = {
        "candidate_id": "candidate-001",
        "identity_key": candidate["identity_key"],
        "outcome": "grounded",
        "reason_code": "",
        "content_description": "Практическое руководство о времени и внимании.",
    }

    row = _render_neutral_row(
        candidate,
        stale_row,
        {},
        grounded_stage_record=grounded_stage_record,
    )

    assert row["editor_row"]["Description reason"] == ""
    assert row["description_stage_reason"] == ""
