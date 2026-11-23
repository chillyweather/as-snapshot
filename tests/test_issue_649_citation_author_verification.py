"""Offline coverage for issue #649: citation author verification.

Citation verification proves the adapter did not invent a cited page. It
does that by re-fetching the page and checking that every printed author
it claimed really occurs there. Two things went wrong in the first live
run of the production adapter (#638).

The bounded fetch harvested authors only from a schema.org ``Book``
record, so an ``Audiobook`` record carried its author past the parser
unread. The page then presented no author at all, every printed author
failed, and the citation was dropped as ``author_mismatch``: a page that
names a different author and a page that names no author are different
facts, and only the first one is a mismatch.

Every test here is offline. The pages are the real shapes taken from the
two hosts in the live run, reduced to the elements the parser reads.
"""

from __future__ import annotations

import json
from typing import Any

from autoscout.description_adjudication import REASON_AUTHOR_UNVERIFIABLE
from autoscout.description_adjudication import verify_adjudication_response
from autoscout.grounded_description import REASON_AUTHOR_MISMATCH
from autoscout.grounded_description import BoundedHTTPFetcher

AUDIOBOOK_URL = "https://books.apple.com/us/audiobook/chemistry-and-our-universe/id1458573458"
NO_AUTHOR_URL = "https://shop.example-courses.com/chemistry-and-our-universe"
WRONG_AUTHOR_URL = "https://catalogue.example-library.org/chemistry-and-our-universe"

TITLE = "Chemistry and Our Universe: How It All Works"


def _page(*, jsonld: dict[str, Any] | None = None, title: str = TITLE, body: str = "") -> str:
    script = f'<script type="application/ld+json">{json.dumps(jsonld)}</script>' if jsonld else ""
    return f"<html><head><title>{title}</title>{script}</head><body>{body}</body></html>"


# The real Apple Books shape: a schema.org Audiobook record whose author
# names both people, joined the way the page joins them, and HTML-escaped
# the way the page escapes the JSON-LD it embeds. Both details are taken
# from the live page, and each one alone was enough to lose the author.
AUDIOBOOK_PAGE = _page(
    jsonld={
        "@type": "Audiobook",
        "name": TITLE,
        "author": "Ron B. Davis &amp; The Great Courses",
        "description": "A survey of chemistry from the periodic table to plate tectonics.",
    }
)

# The real Great Courses shape: the author exists only inside a page
# script the bounded parser does not read, so the page presents none.
NO_AUTHOR_PAGE = _page(body=f"<main><p>{TITLE} is an award-winning course covering chemistry.</p></main>")

WRONG_AUTHOR_PAGE = _page(
    jsonld={"@type": "Book", "name": TITLE, "author": "Someone Else Entirely", "description": "A different book."}
)


class _PageTransport:
    """Serve the three fixed pages; never reach the network."""

    def __init__(self) -> None:
        self.pages = {
            AUDIOBOOK_URL: AUDIOBOOK_PAGE,
            NO_AUTHOR_URL: NO_AUTHOR_PAGE,
            WRONG_AUTHOR_URL: WRONG_AUTHOR_PAGE,
        }

    def request(self, url: str, **_kwargs: Any) -> Any:
        from autoscout.grounded_description import HTTPResponse

        for known, page in self.pages.items():
            if url.rstrip("/") == known.rstrip("/"):
                return HTTPResponse(
                    url=url, status=200, headers={"content-type": "text/html"}, body=page.encode("utf-8")
                )
        raise AssertionError(f"offline test fetched an unexpected url: {url}")


def _fetcher() -> BoundedHTTPFetcher:
    return BoundedHTTPFetcher(transport=_PageTransport())  # type: ignore[arg-type]


def _response(url: str, printed_authors: list[str]) -> dict[str, Any]:
    return {
        "identity_key": "chemistry and our universe|ron b davis",
        "resolved": True,
        "unresolved_reason": "",
        "confidence": "high",
        "content_description": "Аудиокурс о химии, от периодической таблицы до тектоники плит.",
        "citations": [
            {
                "url": url,
                "host": url.split("/")[2],
                "surface_kind": "retail_listing",
                "printed_title": TITLE,
                "printed_authors": printed_authors,
                "describes_this_work": True,
                "identity_note": "",
            }
        ],
    }


def _verify(url: str, printed_authors: list[str]) -> dict[str, Any]:
    return verify_adjudication_response(
        _response(url, printed_authors),
        candidate_id="candidate-001",
        identity_key="chemistry and our universe|ron b davis",
        fetcher=_fetcher(),
        checked_date="2026-09-03",
    )


def test_an_audiobook_record_yields_its_author_so_the_citation_verifies() -> None:
    """A schema.org Audiobook is a Book. Its author must be read.

    This is the defect the live run hit: the citation was correct, the
    page named the author, and the parser never looked at the record.
    """
    verification = _verify(AUDIOBOOK_URL, ["Ron B. Davis", "The Great Courses"])

    assert verification["dropped"] == []
    assert verification["owner_count"] == 1
    assert verification["counting_urls"] == [AUDIOBOOK_URL]


def test_a_page_that_names_no_author_is_unverifiable_not_a_mismatch() -> None:
    """ "No author on the page" and "a different author" are different facts.

    Both drop the citation. Only one of them is evidence that the adapter
    named the wrong book, and an editor has to be able to tell them apart.
    """
    verification = _verify(NO_AUTHOR_URL, ["Ron B. Davis Jr."])

    assert [drop["reason"] for drop in verification["dropped"]] == [REASON_AUTHOR_UNVERIFIABLE]
    assert verification["owner_count"] == 0


def test_a_page_that_names_a_different_author_is_still_a_mismatch() -> None:
    # The guard itself must not weaken: a page about a different author is
    # exactly what citation verification exists to catch.
    verification = _verify(WRONG_AUTHOR_URL, ["Ron B. Davis"])

    assert [drop["reason"] for drop in verification["dropped"]] == [REASON_AUTHOR_MISMATCH]
    assert verification["owner_count"] == 0
