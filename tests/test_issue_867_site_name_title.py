"""A page title that opens with the site name keeps the book title (#867)."""

from __future__ import annotations

from typing import Any

import pytest

from autoscout.grounded_description import REASON_TITLE_MISMATCH
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import FetchedSource
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import _identity_reason

TARGET = DurableTarget("animals", "Animals Taught Me Everything", ("Pam Houston",), {})
URL = "https://www.torreyhouse.org/animals-taught-me-everything"
SUBJECT = "Pam Houston has learned life's most important lessons from animals. How to play. How to rest."


class PageTransport:
    def __init__(self, html: str) -> None:
        self.html = html

    def request(self, url: str, **kwargs: Any) -> HTTPResponse:
        return HTTPResponse(url=url, status=200, headers={"content-type": "text/html"}, body=self.html.encode())


def fetch(html: str, url: str = URL) -> FetchedSource:
    fetcher = BoundedHTTPFetcher(transport=PageTransport(html), min_host_interval_seconds=0)
    return fetcher.fetch(
        DiscoveryResult(url, "publisher", "publisher", "torreyhouse.org", "en"), checked_date="2026-10-27"
    )


def page(title: str, site_name: str = "torrey-house-press") -> str:
    return (
        f'<html><head><title>{title}</title><meta property="og:title" content="{title}"/>'
        f'<meta property="og:site_name" content="{site_name}"/></head>'
        f"<body><main><p>{SUBJECT}</p></main></body></html>"
    )


def test_the_torrey_house_title_shape_keeps_the_book_title_and_author() -> None:
    fetched = fetch(page("Torrey House Press | ANIMALS TAUGHT ME EVERYTHING by Pam Houston"))
    assert fetched.title == "ANIMALS TAUGHT ME EVERYTHING"
    assert fetched.primary_authors == ("Pam Houston",)
    assert _identity_reason(TARGET, fetched) == ""


@pytest.mark.parametrize(
    "title",
    [
        "Torreyhouse | Animals Taught Me Everything",  # matches the host, not og:site_name
        "Torrey House Press - Torreyhouse - Animals Taught Me Everything",  # two leading site labels
    ],
)
def test_leading_site_labels_from_host_or_site_name_are_dropped(title: str) -> None:
    assert fetch(page(title)).title == "Animals Taught Me Everything"


def test_the_usual_book_then_site_order_is_unchanged() -> None:
    assert fetch(page("Animals Taught Me Everything | Torrey House Press")).title == "Animals Taught Me Everything"


def test_a_leading_segment_that_is_not_the_site_is_kept() -> None:
    fetched = fetch(page("Nature Writing | Animals Taught Me Everything"))
    assert fetched.title == "Nature Writing"
    assert _identity_reason(TARGET, fetched) == REASON_TITLE_MISMATCH


def test_a_title_that_is_only_the_site_name_stays_the_site_name() -> None:
    assert fetch(page("Torrey House Press")).title == "Torrey House Press"
