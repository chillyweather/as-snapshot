"""ISBN evidence names a fetched book independently of title/author wording (#755)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from autoscout.grounded_description import REASON_AUTHOR_MISMATCH
from autoscout.grounded_description import REASON_SOURCE_IDENTITY_DISAGREEMENT
from autoscout.grounded_description import REASON_SUBJECT_DISAGREEMENT
from autoscout.grounded_description import REASON_TITLE_MISMATCH
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import FetchedSource
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import _identity_reason
from autoscout.grounded_description import evaluate_grounding_gate

ISBN = "9781631494536"
SUBJECT = "The book examines government housing segregation and its history."
TARGET = DurableTarget("color-of-law", "The Color of Law", ("Richard Rothstein",), {"isbn": ISBN})


def source(owner: str = "one", **changes: Any) -> FetchedSource:
    return replace(
        FetchedSource(
            url=f"https://{owner}.example/book",
            source_kind="retailer",
            source_family="retailer",
            owner_id=owner,
            source_language="en",
            title=TARGET.title,
            primary_authors=TARGET.primary_authors,
            subject_text=SUBJECT,
            checked_date="2026-10-27",
        ),
        **changes,
    )


@pytest.mark.parametrize("title", ["Color", "The Color of Law: A Forgotten History", "Different wording", ""])
@pytest.mark.parametrize("authors", [(), ("ROTHSTEIN, RICHARD",), ("Different name",)])
@pytest.mark.parametrize("isbn", [ISBN, "978-1-63149-453-6", "1631494538"])
def test_matching_isbn_proves_single_source_identity(title: str, authors: tuple[str, ...], isbn: str) -> None:
    fetched = source(title=title, primary_authors=authors, isbn=isbn)
    assert _identity_reason(TARGET, fetched) == ""
    assert evaluate_grounding_gate(TARGET, [fetched]).accepted


def test_candidate_isbn10_and_lowercase_x_normalize_to_the_same_isbn13() -> None:
    target = replace(TARGET, record={"isbn": "0-8044-2957-x"})
    assert evaluate_grounding_gate(target, [source(isbn="9780804429573", title="Other", primary_authors=())]).accepted


def test_different_plausible_isbn_is_a_conflict_on_a_title_only_match() -> None:
    other = source(isbn="9780306406157", primary_authors=())
    assert _identity_reason(TARGET, other) == REASON_SOURCE_IDENTITY_DISAGREEMENT
    assert not evaluate_grounding_gate(TARGET, [other]).accepted
    result = evaluate_grounding_gate(TARGET, [source(), other])
    assert not result.accepted
    assert result.reason_code == REASON_SOURCE_IDENTITY_DISAGREEMENT


def test_different_isbn_with_the_target_author_is_another_edition() -> None:
    """A hardcover page for a paperback candidate keeps the title/author checks."""
    edition = source(isbn="9780306406157", primary_authors=("Richard Rothstein",))
    assert _identity_reason(TARGET, edition) == ""
    assert evaluate_grounding_gate(TARGET, [edition]).accepted
    wrong_author = source(isbn="9780306406157", primary_authors=("Different name",))
    assert _identity_reason(TARGET, wrong_author) == REASON_SOURCE_IDENTITY_DISAGREEMENT


def test_isbn_sources_agree_with_each_other_and_with_text_verified_peers() -> None:
    first = source(isbn=ISBN, title="A longer title with different wording", primary_authors=())
    second = source("two", isbn="1631494538", title="Color", primary_authors=("Other",))
    third = source("three")
    for sources in ([first, second], [first, third], [first, second, third]):
        result = evaluate_grounding_gate(TARGET, sources)
        assert result.accepted
        assert len(result.accepted_sources) == len(sources)


def test_isbn_proof_does_not_launder_a_conflicting_peer() -> None:
    result = evaluate_grounding_gate(
        TARGET,
        [source(isbn=ISBN, title="Color", primary_authors=()), source("two", primary_authors=("Other",))],
    )
    assert not result.accepted
    assert result.reason_code == REASON_SOURCE_IDENTITY_DISAGREEMENT


def test_isbn_singleton_can_be_selected_without_an_extracted_author() -> None:
    result = evaluate_grounding_gate(
        TARGET,
        [source(isbn=ISBN, title="Color", primary_authors=()), source("two", title="Unrelated book")],
    )
    assert result.accepted
    assert len(result.accepted_sources) == 1
    assert result.accepted_sources[0].isbn == ISBN


def test_matching_isbn_keeps_subject_and_policy_checks() -> None:
    first = source(isbn=ISBN, primary_authors=(), subject_key="housing segregation")
    other = source("two", isbn=ISBN, subject_key="volcanic geology")
    assert evaluate_grounding_gate(TARGET, [first, other]).reason_code == REASON_SUBJECT_DISAGREEMENT
    for rejected in (
        replace(first, subject_text="", subject_key=""),
        replace(first, classification="unknown"),
        replace(first, is_search_snippet=True),
        replace(first, checked_date=""),
    ):
        assert not evaluate_grounding_gate(TARGET, [rejected]).accepted


@pytest.mark.parametrize("candidate_isbn", ["", "978-1", "garbage"])
def test_candidates_without_a_plausible_isbn_keep_the_old_gate(candidate_isbn: str) -> None:
    target = replace(TARGET, record={"isbn": candidate_isbn})
    assert evaluate_grounding_gate(target, [source(isbn="9780306406157")]).accepted
    assert _identity_reason(target, source(isbn=ISBN, title="Other")) == REASON_TITLE_MISMATCH
    assert _identity_reason(target, source(isbn=ISBN, primary_authors=("Other",))) == REASON_AUTHOR_MISMATCH
    assert not evaluate_grounding_gate(target, [source(isbn=ISBN, primary_authors=())]).accepted


def test_implausible_source_isbn_does_not_supply_proof() -> None:
    assert _identity_reason(TARGET, source(isbn="978-1", title="Other")) == REASON_TITLE_MISMATCH


class PageTransport:
    def __init__(self, html: str, final_url: str = "") -> None:
        self.html = html
        self.final_url = final_url
        self.requests: list[str] = []

    def request(self, url: str, **kwargs: Any) -> HTTPResponse:
        self.requests.append(url)
        return HTTPResponse(
            url=self.final_url or url,
            status=200,
            headers={"content-type": "text/html"},
            body=self.html.encode(),
        )


def fetch(url: str, html: str = "", final_url: str = "") -> tuple[FetchedSource, PageTransport]:
    transport = PageTransport(html or f"<title>Color</title><main><p>{SUBJECT}</p></main>", final_url)
    fetched = BoundedHTTPFetcher(transport=transport, min_host_interval_seconds=0).fetch(
        DiscoveryResult(url, "retailer", "retailer", "one", "en"), checked_date="2026-10-27"
    )
    assert len(transport.requests) == 1
    return fetched, transport


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.amazon.com/Color-Law/dp/1631494538", ISBN),
        ("https://www.amazon.com/gp/product/1631494538/ref=abc", ISBN),
        ("https://www.amazon.co.uk/dp/080442957X/", "9780804429573"),
        ("https://www.amazon.com/dp/080442957x", "9780804429573"),
        (f"https://shop.example/products/isbn/{ISBN}", ISBN),
        (f"https://shop.example/products/color-of-law-{ISBN}", ISBN),
        (f"https://shop.example/products/{ISBN}", ISBN),
        ("https://www.amazon.com/dp/B0G4P2VQ4P", ""),
        ("https://www.amazon.com/dp/1631494539", ""),  # Numeric ASIN with bad ISBN-10 checksum.
        ("https://www.amazon.com/dp/16314945380", ""),
        ("https://shop.example/1631494538", ""),
        (f"https://shop.example/book?isbn={ISBN}", ""),
        (f"https://shop.example/book#isbn={ISBN}", ""),
        (f"https://shop.example/1{ISBN}0", ""),
        (f"https://shop.example/{ISBN}/9780306406157", ""),
    ],
)
def test_fetcher_extracts_url_isbn_but_not_non_isbn_asins(url: str, expected: str) -> None:
    fetched, _ = fetch(url, f"<title>The Color of Law</title><main><p>{SUBJECT}</p></main>")
    # The address ISBN stays apart from the page's stated ISBN, so it is
    # never learned into the candidate.
    assert fetched.isbn == ""
    assert fetched.url_isbn == expected
    if expected == ISBN:
        assert evaluate_grounding_gate(TARGET, [fetched]).accepted


def test_page_isbn_takes_precedence_over_url() -> None:
    html = """<script type="application/ld+json">{"@type":"Book", "name":"The Color of Law",
      "isbn":"9780306406157", "description":"Government housing segregation history"}</script>"""
    fetched, _ = fetch(f"https://shop.example/products/{ISBN}", html)
    assert fetched.isbn == "9780306406157"
    assert _identity_reason(TARGET, fetched) == REASON_SOURCE_IDENTITY_DISAGREEMENT


def test_only_the_fetched_response_url_can_supply_isbn() -> None:
    fetched, _ = fetch(f"https://shop.example/products/{ISBN}", final_url="https://shop.example/landing")
    assert fetched.url_isbn == ""


def test_visible_page_isbn_supplies_identity_without_structured_metadata() -> None:
    fetched, _ = fetch(
        "https://shop.example/book",
        f"<title>Color</title><main><p>{SUBJECT}</p><div>ISBN: 978-1-63149-453-6</div></main>",
    )
    assert fetched.isbn == ""
    assert evaluate_grounding_gate(TARGET, [fetched]).accepted


def test_structured_page_isbn_proves_identity_when_author_and_title_differ() -> None:
    html = f'''<script type="application/ld+json">{{"@type":"Book", "name":"Color",
      "author":{{"@type":"Person", "name":"ROTHSTEIN, RICHARD"}},
      "isbn":"{ISBN}", "description":"{SUBJECT}"}}</script>'''
    fetched, _ = fetch("https://shop.example/book", html)
    assert fetched.isbn == ISBN
    assert evaluate_grounding_gate(TARGET, [fetched]).accepted


def test_isbn_proof_does_not_turn_a_silent_peer_into_identity_evidence() -> None:
    proven = source(isbn=ISBN, primary_authors=())
    silent = source("two", primary_authors=(), subject_text="The history of housing segregation in America.")
    result = evaluate_grounding_gate(TARGET, [proven, silent])
    assert result.accepted
    assert [item.canonical_url for item in result.accepted_sources] == [proven.url]
    silent_only = evaluate_grounding_gate(TARGET, [source(primary_authors=()), silent])
    assert not silent_only.accepted
    assert silent_only.reason_code == REASON_SOURCE_IDENTITY_DISAGREEMENT


def test_a_thirteen_digit_path_id_without_an_isbn_check_digit_is_not_an_isbn() -> None:
    from autoscout.grounded_description import _url_isbn

    assert _url_isbn("https://shop.example/products/the-color-of-law-9781631494536") == "9781631494536"
    assert _url_isbn("https://shop.example/products/item-9781631494537") == ""


def test_a_discovery_only_learned_isbn_proves_nothing() -> None:
    learned = replace(TARGET, record={"isbn": ISBN, "isbn_discovery_only": True})
    other_work = source(isbn=ISBN, title="Another Book", primary_authors=("Someone Else",))
    assert _identity_reason(learned, other_work) == REASON_TITLE_MISMATCH
    assert not evaluate_grounding_gate(learned, [other_work]).accepted


def test_isbns_differing_only_in_the_check_digit_are_not_the_same_book() -> None:
    other = source(isbn="9781631494537", title="Another Book", primary_authors=("Someone Else",))
    assert not evaluate_grounding_gate(TARGET, [other]).accepted


def test_a_search_address_carrying_the_isbn_is_not_proof() -> None:
    from autoscout.grounded_description import _url_isbn

    assert _url_isbn("https://www.amazon.com/s/9781631494536") == ""
    assert _url_isbn("https://shop.example/search/9781631494536") == ""
    assert _url_isbn("https://www.amazon.com/Color-Law/dp/1631494538") == "9781631494536"


def test_a_lone_isbn_conflict_is_reported_as_an_identity_conflict() -> None:
    other = source(isbn="9780306406157", primary_authors=())
    result = evaluate_grounding_gate(TARGET, [other])
    assert not result.accepted
    assert result.reason_code == REASON_SOURCE_IDENTITY_DISAGREEMENT


def test_isbn_proof_does_not_bypass_the_verified_title_route_evidence() -> None:
    """The stage artifact refuses this route without mapping evidence on persistence."""
    unverified = source(isbn=ISBN, title_evidence_route="verified_published_title", title_evidence=None)
    assert _identity_reason(TARGET, unverified) == REASON_TITLE_MISMATCH
    assert not evaluate_grounding_gate(TARGET, [unverified]).accepted


def test_a_challenge_page_at_the_books_address_is_not_proof() -> None:
    challenge = (
        "<title>Amazon.com</title><main><p>Enter the characters you see below. "
        "Sorry, we just need to make sure you are not a robot.</p></main>"
    )
    fetched, _ = fetch("https://www.amazon.com/dp/1631494538", challenge)
    assert fetched.url_isbn == ISBN
    assert not evaluate_grounding_gate(TARGET, [fetched]).accepted


def test_a_different_address_isbn_is_no_conflict() -> None:
    edition = source(url_isbn="9780306406157", primary_authors=())
    assert _identity_reason(TARGET, edition) == ""
