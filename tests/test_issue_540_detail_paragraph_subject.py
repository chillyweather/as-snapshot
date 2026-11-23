"""RED reproduction for issue #540 detail-subject container losing paragraph text."""

from __future__ import annotations

from pathlib import Path

from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import FetchedSource
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import committed_source_classifier
from autoscout.grounded_description import evaluate_grounding_gate

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "issue-540-detail-paragraph-subject"
HTML_PATH = FIXTURE_DIR / "barnesandnoble-this-moment-matters.html"
URL = "https://www.barnesandnoble.com/w/this-moment-matters-ruth-chou-simons/1148674519"


class FixtureTransport:
    def request(self, url: str, **_: object) -> HTTPResponse:
        return HTTPResponse(
            url,
            200,
            {"content-type": "text/html; charset=utf-8"},
            HTML_PATH.read_bytes(),
        )


def _fetched():
    classifier = committed_source_classifier()
    meta = classifier.classify(URL)
    result = DiscoveryResult(
        url=URL,
        source_kind=meta["source_kind"],
        source_family=meta["source_family"],
        owner_id=meta["owner_id"],
        source_language="en",
    )
    fetcher = BoundedHTTPFetcher(transport=FixtureTransport())
    return fetcher.fetch(result, checked_date="2026-08-20")


def test_issue_540_barnesandnoble_yields_real_description() -> None:
    fetched = _fetched()
    # Before fix this is 84 characters and fails both assertions.
    # After fix it must be the full product description, not the leftover teaser.
    assert len(fetched.subject_text) >= 200, fetched.subject_text
    assert "Ruth Chou Simons" in fetched.subject_text, fetched.subject_text
    assert "This Moment Matters" in fetched.subject_text, fetched.subject_text


def test_issue_540_barnesandnoble_excludes_interface_text() -> None:
    fetched = _fetched()
    assert "Read More" not in fetched.subject_text
    assert "Read More" not in fetched.display_text


def _make_source(
    url: str,
    title: str,
    subject: str,
    authors: tuple[str, ...],
    owner: str,
) -> FetchedSource:
    return FetchedSource(
        url=url,
        source_kind="review",
        source_family="book-review",
        owner_id=owner,
        source_language="en",
        title=title,
        primary_authors=authors,
        subject_text=subject,
        subject_key=subject,
        checked_date="2026-08-20",
        classification="classified",
    )


def test_issue_540_gate_accepts_this_moment_matters_with_silent_barnes() -> None:
    # Use real Candidate History values for this moment matters.
    # Title is the full title from history, not an invented value.
    target = DurableTarget(
        "this moment matters|ruth chou simons",
        "This Moment Matters: 100 Ways to Cultivate a Grace-Laced Life",
        ("Ruth Chou Simons",),
        {},
    )
    title = "This Moment Matters: 100 Ways to Cultivate a Grace-Laced Life"
    strong_subject = (
        "Ruth Chou Simons shares how recognizing grace can make everyday life "
        "special with suggestions for six areas of life including beauty and grace. "
    ) * 5
    weak_subject = "You can live a beautiful life filled with meaning and purpose. Looking ... " * 2
    # Two strong sources carry author evidence, the third is silent on identity.
    # Use the real same-owner production shape (all publisher-supplied-copy) so
    # the gate must use the agreeing-subset fallback.
    amazon = _make_source(
        "https://www.amazon.com/dp/1400244439",
        title,
        strong_subject,
        (),
        "publisher-supplied-copy",
    )
    us_amazon = _make_source(
        "https://us.amazon.com/dp/B0G1JZT1RW",
        title,
        strong_subject,
        ("Ruth Chou Simons",),
        "publisher-supplied-copy",
    )
    barnes = _make_source(
        URL,
        title,
        weak_subject,
        (),
        "publisher-supplied-copy",
    )
    # Gate must accept despite the silent third. Before #540 this was
    # REFUSED with source_identity_disagreement.
    result = evaluate_grounding_gate(target, [amazon, us_amazon, barnes])
    assert result.accepted is True
    assert result.reason_code == ""
    # The silent source must not be in the accepted set.
    accepted_urls = {s.canonical_url for s in result.accepted_sources}
    assert URL not in accepted_urls
    # At least one strong source must be accepted.
    assert "https://us.amazon.com/dp/B0G1JZT1RW" in accepted_urls


def test_issue_540_gate_accepts_togetherness_with_silent_ebook() -> None:
    # Real Candidate History values for togetherness.
    # Note the title is truncated with a Unicode ellipsis as stored.
    target = DurableTarget(
        "togetherness|rowan hooper",
        "Togetherness: Symbiosis and the Hidden Story of …",
        ("Rowan Hooper",),
        {},
    )
    # Strong subjects mention the author and share the symbiosis vocabulary.
    strong_subject = (
        "Rowan Hooper explores symbiosis and the hidden story of life "
        "collaborations with biology and togetherness insights. "
    ) * 5
    # Weak ebook subject is a real edition description without the author name.
    weak_subject = (
        "How symbiosis can change the way we understand our world ourselves "
        "and nature with biology togetherness hidden story. "
    ) * 5
    # Use same-owner production shape for the critical veto case.
    amazon = _make_source(
        "https://www.amazon.com/dp/0593802667",
        "Togetherness: Symbiosis and the Hidden Story of Life's Greatest Collaborations",
        strong_subject,
        (),
        "publisher-supplied-copy",
    )
    barnes = _make_source(
        "https://www.barnesandnoble.com/w/togetherness-rowan-hooper/1148893932",
        "Togetherness: Symbiosis and the Hidden Story of Life's Greatest Collaborations",
        strong_subject,
        ("Rowan Hooper",),
        "publisher-supplied-copy",
    )
    ebook = _make_source(
        "https://www.amazon.com/dp/B0FSCJJZ2W",
        "Togetherness: How symbiosis can change the way we understand our world, ourselves...",
        weak_subject,
        (),
        "publisher-supplied-copy",
    )
    result = evaluate_grounding_gate(target, [amazon, barnes, ebook])
    assert result.accepted is True
    assert result.reason_code == ""
    accepted_urls = {s.canonical_url for s in result.accepted_sources}
    # The silent ebook must not be in the accepted set.
    assert "https://www.amazon.com/dp/B0FSCJJZ2W" not in accepted_urls
    # At least one strong source must be accepted.
    assert (
        "https://www.amazon.com/dp/0593802667" in accepted_urls
        or "https://www.barnesandnoble.com/w/togetherness-rowan-hooper/1148893932" in accepted_urls
    )


def test_issue_540_conflicting_author_still_vetoes() -> None:
    target = DurableTarget(
        "exhumed|aaron mahnke",
        "Exhumed: Unearthing the History of the American Vampire",
        ("Aaron Mahnke",),
        {},
    )
    good = _make_source(
        "https://good.example.test/book",
        "Exhumed: Unearthing the History of the American Vampire",
        "An engrossing exploration by Aaron Mahnke with symbiosis and history.",
        ("Aaron Mahnke",),
        "owner-good",
    )
    # This source names a different author - genuine conflict, must keep veto.
    conflicting = _make_source(
        "https://conflict.example.test/book",
        "Exhumed: Unearthing the History of the American Vampire",
        "An engrossing exploration with symbiosis and history.",
        ("Robert Harris",),
        "owner-conflict",
    )
    result = evaluate_grounding_gate(target, [good, conflicting])
    assert result.accepted is False
    assert result.reason_code == "source_identity_disagreement"
