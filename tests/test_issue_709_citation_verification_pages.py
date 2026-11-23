"""Verification must not drop a correct citation for a fetcher reason (issue #709).

In the 2026-09-16 weekly run the adjudication agent cited the right pages for
*Coragem de transformar* and *Beyond Quick Fixes*, and code verification
dropped them:

- Google Books answered ``books.google.com`` with a redirect to the country
  domain of the machine running the fetch, which the fetcher refuses;
- Livraria Loyola declares ``<meta name="author" content="F-Store">`` (its
  e-commerce platform) on every page, so the real author printed on the page
  lost to the platform name and the citation was dropped as ``author_mismatch``;
- the printed-author check only saw the title and a meta description, so the
  author link the page prints for the book could not rescue it.

Review of #710 narrowed that rescue: only the first author link outside a
related-books section counts, never a name elsewhere on the page.

The fixtures are trimmed copies of the pages as served on 2026-09-16.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from adjudication_test_support import FakeTransport

from autoscout.description_adjudication import REASON_AUTHOR_MISMATCH
from autoscout.description_adjudication import REASON_AUTHOR_UNVERIFIABLE
from autoscout.description_adjudication import verify_adjudication_response
from autoscout.grounded_description import BoundedHTTPError
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import HTTPResponse

FIXTURES = Path(__file__).parent / "fixtures" / "issue-709"

LOYOLA_URL = "https://www.livrarialoyola.com.br/produto/coragem-de-transformar-a-escolha-que-mudou-tudo-458355"
LOYOLA_HTML = (FIXTURES / "livrarialoyola-coragem-de-transformar.html").read_text(encoding="utf-8")

GOOGLE_URL = "https://books.google.com/books?id=jX3tEQAAQBAJ"
GOOGLE_NO_REDIRECT_URL = "https://books.google.com/books?id=jX3tEQAAQBAJ&redir_esc=y"
GOOGLE_HTML = (FIXTURES / "books-google-beyond-quick-fixes.html").read_text(encoding="utf-8")


class CountryRedirectTransport(FakeTransport):
    """Serve Google Books only where it does not redirect to a country domain."""

    def request(self, url: str, **kwargs: Any) -> HTTPResponse:
        if url == GOOGLE_URL:
            with self._lock:
                self.requested_urls.append(url)
            raise BoundedHTTPError("redirect left the allowed host")
        return super().request(url, **kwargs)


def _loyola_citation(authors: list[str]) -> dict[str, Any]:
    return {
        "url": LOYOLA_URL,
        "host": "livrarialoyola.com.br",
        "surface_kind": "retail_listing",
        "printed_title": "CORAGEM DE TRANSFORMAR: A ESCOLHA QUE MUDOU TUDO",
        "printed_authors": authors,
        "describes_this_work": True,
        "identity_note": "",
    }


def _verify(citations: list[dict[str, Any]], pages: dict[str, str | None], transport: Any = None) -> dict[str, Any]:
    active = transport or FakeTransport(pages)
    return verify_adjudication_response(
        {"identity_key": "work|author", "citations": citations},
        candidate_id="candidate-709",
        identity_key="work|author",
        fetcher=BoundedHTTPFetcher(transport=active),
        checked_date="2026-09-16",
    )


def _fetch(url: str, html: str) -> Any:
    return BoundedHTTPFetcher(transport=FakeTransport({url: html})).fetch(
        {"url": url, "classification": "unknown", "is_search_snippet": False},
        allow_unclassified_host=True,
    )


def test_a_shop_platform_meta_author_is_not_the_book_author() -> None:
    fetched = _fetch(LOYOLA_URL, LOYOLA_HTML)

    assert "F-Store" not in fetched.primary_authors


def test_a_meta_author_the_page_also_prints_is_kept() -> None:
    html = (
        '<html><head><title>Work One</title><meta name="author" content="Alex Author"></head>'
        "<body><main><h1>Work One</h1><p>Written by Alex Author about memory and public life.</p></main></body></html>"
    )

    assert _fetch("https://publisher.example.test/work-one", html).primary_authors == ("Alex Author",)


def test_the_loyola_citation_survives_on_the_author_the_page_prints() -> None:
    verification = _verify([_loyola_citation(["ALCIONE ALBANESI"])], {LOYOLA_URL: LOYOLA_HTML})

    assert verification["dropped"] == []
    assert [item["url"] for item in verification["surviving"]] == [LOYOLA_URL]


def test_a_claimed_author_the_page_does_not_print_is_still_dropped() -> None:
    verification = _verify([_loyola_citation(["Someone Else"])], {LOYOLA_URL: LOYOLA_HTML})

    assert verification["surviving"] == []
    assert [item["reason"] for item in verification["dropped"]] == [REASON_AUTHOR_UNVERIFIABLE]


WORK_ONE_URL = "https://publisher.example.test/work-one"


def _work_one_citation(author: str) -> dict[str, Any]:
    return {**_loyola_citation([author]), "url": WORK_ONE_URL, "printed_title": "Work One"}


def test_an_author_link_never_overrides_an_author_the_page_states() -> None:
    html = (
        "<html><head><title>Work One</title>"
        '<script type="application/ld+json">{"@type": "Book", "name": "Work One", '
        '"author": {"@type": "Person", "name": "Alex Author"}}</script></head>'
        "<body><main><p>Work One is about memory.</p></main>"
        '<a href="/author/blake-writer">Blake Writer</a></body></html>'
    )

    verification = _verify([_work_one_citation("Blake Writer")], {WORK_ONE_URL: html})

    assert verification["surviving"] == []
    assert [item["reason"] for item in verification["dropped"]] == [REASON_AUTHOR_MISMATCH]


def test_a_name_in_a_related_books_section_does_not_validate_a_citation() -> None:
    """Review finding on #710: without structured authors, any page text used to count."""
    html = (
        "<html><head><title>Work One</title></head><body>"
        "<h1>Work One</h1><div>Alex Author</div>"
        '<section class="related-products"><h2>Readers also bought</h2>'
        '<a href="/author/blake-writer">Blake Writer</a> Other Work by Blake Writer</section>'
        "</body></html>"
    )

    verification = _verify([_work_one_citation("Blake Writer")], {WORK_ONE_URL: html})

    assert verification["surviving"] == []
    assert [item["reason"] for item in verification["dropped"]] == [REASON_AUTHOR_UNVERIFIABLE]


def test_a_plain_text_mention_counts_only_beside_the_printed_title() -> None:
    """Rule change (issue #791): this page used to drop as ``author_unverifiable``.

    A page that states no author and prints the cited title now confirms an
    author whose surname its visible text prints: publisher pages name the
    author only in a heading. Without the title the citation still drops.
    """
    html = (
        "<html><head><title>Work One</title></head><body>"
        "<h1>Work One</h1><div>A new book that Alex Author reviewed warmly.</div></body></html>"
    )

    verification = _verify([_work_one_citation("Alex Author")], {WORK_ONE_URL: html})
    untitled = _verify([{**_work_one_citation("Alex Author"), "printed_title": "Another Work"}], {WORK_ONE_URL: html})

    assert [item["url"] for item in verification["surviving"]] == [WORK_ONE_URL]
    assert untitled["surviving"] == []


def test_only_the_first_author_link_outside_related_sections_is_the_book_author() -> None:
    html = (
        "<html><head><title>Work One</title></head><body>"
        '<div id="carousel-recommended"><div><a href="/autor/blake-writer">Blake Writer</a></div></div>'
        '<h1>Work One</h1><a class="book-author" href="/autor/alex-author">Alex Author</a>'
        '<a href="/autor/casey-other">Casey Other</a>'
        "</body></html>"
    )

    kept = _verify([_work_one_citation("Alex Author")], {WORK_ONE_URL: html})
    later = _verify([_work_one_citation("Casey Other")], {WORK_ONE_URL: html})
    related = _verify([_work_one_citation("Blake Writer")], {WORK_ONE_URL: html})

    assert [item["url"] for item in kept["surviving"]] == [WORK_ONE_URL]
    # Rule change (issue #791): a later author link is visible text on a
    # page that prints the title and states no author, so it now counts. A
    # name inside a related-books section still never does.
    assert [item["url"] for item in later["surviving"]] == [WORK_ONE_URL]
    assert related["surviving"] == []


@pytest.mark.parametrize(
    "hidden_link",
    [
        '<a hidden href="/author/blake-writer">Blake Writer</a>',
        '<a aria-hidden="true" href="/author/blake-writer">Blake Writer</a>',
        '<a style="display: none" href="/author/blake-writer">Blake Writer</a>',
        '<a style="visibility:hidden" href="/author/blake-writer">Blake Writer</a>',
        '<a class="visually-hidden" href="/author/blake-writer">Blake Writer</a>',
        '<div hidden><span><a href="/author/blake-writer">Blake Writer</a></span></div>',
        '<div style="display:none"><a href="/author/blake-writer">Blake Writer</a></div>',
        '<ul class="d-none"><li><a href="/author/blake-writer">Blake Writer</a></li></ul>',
    ],
)
def test_a_hidden_author_link_is_not_printed_author_evidence(hidden_link: str) -> None:
    """Review finding on #710: the first-link selector ignored visibility."""
    html = (
        "<html><head><title>Work One</title></head><body>"
        f"<h1>Work One</h1>{hidden_link}"
        '<p>By <a href="/author/alex-author">Alex Author</a></p></body></html>'
    )

    hidden = _verify([_work_one_citation("Blake Writer")], {WORK_ONE_URL: html})
    visible = _verify([_work_one_citation("Alex Author")], {WORK_ONE_URL: html})

    assert hidden["surviving"] == []
    assert [item["reason"] for item in hidden["dropped"]] == [REASON_AUTHOR_UNVERIFIABLE]
    # The visible author link after the hidden one is still the book's author.
    assert [item["url"] for item in visible["surviving"]] == [WORK_ONE_URL]


def test_author_link_text_matches_whole_words_only() -> None:
    html = (
        "<html><head><title>Work One</title></head><body>"
        '<h1>Work One</h1><a href="/author/joanna-annexe">Joanna Annexe</a></body></html>'
    )

    verification = _verify([_work_one_citation("Ann")], {WORK_ONE_URL: html})

    assert [item["reason"] for item in verification["dropped"]] == [REASON_AUTHOR_UNVERIFIABLE]


def test_google_books_is_fetched_without_the_country_redirect() -> None:
    transport = CountryRedirectTransport({GOOGLE_NO_REDIRECT_URL: GOOGLE_HTML})
    citation = {
        "url": GOOGLE_URL,
        "host": "books.google.com",
        "surface_kind": "library_catalogue",
        "printed_title": "Beyond Quick Fixes: Essential Ingredients for Better Mental Health and a Fulfilling Life",
        "printed_authors": ["Celeste Birkhofer"],
        "describes_this_work": True,
        "identity_note": "",
    }

    verification = _verify([citation], {}, transport)

    assert transport.requested_urls == [GOOGLE_NO_REDIRECT_URL]
    assert verification["dropped"] == []
    assert len(verification["surviving"]) == 1


def test_only_google_books_urls_get_the_redirect_parameter() -> None:
    transport = FakeTransport({"https://books.example.test/books?id=1": "<html><title>x</title></html>"})
    fetcher = BoundedHTTPFetcher(transport=transport)
    try:
        fetcher.fetch(
            {"url": "https://books.example.test/books?id=1", "classification": "unknown"},
            allow_unclassified_host=True,
        )
    except BoundedHTTPError:
        pass

    assert transport.requested_urls == ["https://books.example.test/books?id=1"]
