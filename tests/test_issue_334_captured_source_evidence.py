"""Tests for Issue #334: pure evaluation of captured Source Candidate evidence.

``evaluate_captured_source_evidence`` must judge already-captured HTTP/page
evidence as a pure, deterministic function with no network, model,
configuration, weekly-state, or Git access. The verdict distinguishes accept,
defer, and reject, and carries a machine-readable reason code, a plain-language
reason, the detected heading, the bounded entry count, and up to three
grounded title/author/URL samples.

Policy pinned here (conservative and fail-closed):

* Clean acceptance: heading + server-rendered linked entries with visible
  primary authors in actual per-entry containers (``li``/``article``/``tr``),
  GET 200 on the requested page. Any non-empty bounded list qualifies;
  the issue specifies no minimum entry count.
* HEAD and GET status evidence are both mandatory: a body-only capture or
  any other missing status rejects before parsing; there is no implicit 200.
  HEAD 405 and 501 permit exactly one plain GET fallback; HEAD 403, 429,
  CAPTCHA/bot walls, and other 5xx never fall back.
* The resolved URL must match the requested scheme, netloc, path after
  harmless trailing-slash normalization, and query; a same-site redirect to
  a different path (``/nonfiction-shortlist`` to ``/fiction-bestsellers``)
  or to the same path with a different query (``/list?category=nonfiction``
  to ``/list?category=fiction``) rejects as ``unrelated_redirect``.
* Anchors inside navigation, header, or footer landmarks (``nav``, ``header``,
  ``footer`` elements and ``role="navigation"/"banner"/"contentinfo"``) are
  ignored when qualifying book entries are found: site chrome is never a
  book entry and never causes a missing-author rejection.
* One bounded entry container (``li``/``article``/``tr``) qualifies at most
  one primary book-level link: a linked author or a ``Buy now`` button inside
  the same container is not a second book entry, and a container that holds
  only action/author links is not an accepted book entry.
* JS-only pages defer (``js_only``); bot walls (including a bare visible
  CAPTCHA heading), bad HTTP, unrelated redirects, missing headings,
  unlinked or unbounded lists (including a direct-anchor ``section``
  wrapper), and missing authors reject. Narrative prose such as "Winner of
  the annual prize" or "A sweeping history of Europe" is not a primary
  author: the fallback author candidate is a contiguous span, never a word
  reconstruction.
* The primary sample always uses a grounded book-title link and book URL:
  an unmarked author link before the title link is still an author link
  (author-page URL evidence, including profile/bio routes), never the book
  entry. An entry whose book-kind anchors point at several distinct URLs
  has no deterministic primary and rejects fail-closed instead of
  accepting the wrong title/author/URL. Single-word action labels match
  only exactly, so a title such as "Read Write Own" is never dropped.
  Parser metadata classifies action, cover, and author links so primary-link
  selection excludes non-book links, while grounded author evidence is
  parsed separately and unmarked context remains fail-closed (Issue #334
  review).
* Navigation landmarks (``nav``/``role="navigation"``) always filter, even
  inside an actual per-entry container; only an entry's own
  header/footer/banner/contentinfo remains content
  (``<article><header>…</header></article>`` qualifies) (Issue #334
  review).
* The fallback author span is Unicode-safe and name-like: "Łukasz
  Orbitowski" keeps its first character, decomposed accents ("Lúkasz
  Orbitowski") stay in the visible spelling, Russian/Latin names parse,
  and descriptive prose such as "Powerful exploration across Europe",
  "Powerful Exploration Across Europe", or "powerful exploration" is
  rejected deterministically - capitalization alone is never author
  evidence. Grounded author structure (an explicit By/Author marker or
  an author-marked element) relaxes the shape rules: lowercase names
  ("bell hooks") and ordinary multiword names ("Mary Ann Evans",
  "Gabriel Garcia Marquez") are accepted there, while narrative and
  connector prose still fails closed (Issue #334 review).
* Accessible cover labels ("Cover", "Cover of Quantum Worlds", "View
  cover image") are parser-classified non-book links: primary-link selection
  excludes a cover label from the book title. Duplicate anchors that share
  one book URL stay one book link, and the most title-like non-cover anchor
  is the primary title. Grounded author evidence is parsed separately and
  unmarked context remains fail-closed (Issue #334 review).
* Samples are grounded in the captured HTML, deterministic, capped at three,
  and carry title, author, and URL.

All fixtures are local: no live network calls.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoscout.http_evidence import HttpStatusEvidence
from autoscout.source_collectors.base import likely_blocked_response
from autoscout.source_feasibility import OUTCOME_ACCEPT
from autoscout.source_feasibility import OUTCOME_DEFER
from autoscout.source_feasibility import OUTCOME_REJECT
from autoscout.source_feasibility import REASON_ENTRY_SHAPE
from autoscout.source_feasibility import REASON_VISIBLE_TITLE_SELECTOR
from autoscout.source_feasibility import CapturedSourceEvidence
from autoscout.source_feasibility import EntrySample
from autoscout.source_feasibility import FeasibilityVerdict
from autoscout.source_feasibility import evaluate_audible_chart_feasibility
from autoscout.source_feasibility import evaluate_captured_source_evidence
from autoscout.source_feasibility import evidence_from_mapping
from autoscout.source_feasibility import head_fallback_allowed
from autoscout.source_feasibility import validate_http_status_evidence
from autoscout.source_page_identity import LINK_KIND_ACTION
from autoscout.source_page_identity import LINK_KIND_AUTHOR
from autoscout.source_page_identity import VisiblePageIdentityResult
from autoscout.source_page_identity import is_action_label
from autoscout.source_page_identity import is_cover_label
from autoscout.source_page_identity import parse_visible_page_identity
from autoscout.source_page_identity import server_rendered_list_entries
from autoscout.source_page_identity import validate_visible_title_selector

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "issue-334-captured-source-evidence"

SOURCE_URL = "https://example.test/shortlist"

AUTHORS = ["Jane Author", "John Writer", "Ann Editor", "Bob Proofreader", "Eve Reviewer", "Ian Indexer"]


def _entry(title: str, author: str, url: str, *, linked: bool = True) -> str:
    href = f' href="{url}"' if linked else ""
    author_span = f'<span class="author"> {author} </span>' if author else ""
    return f"<li><a{href}>{title}</a>{author_span}</li>"


def _page(*, heading: str = "The 2026 Prize Shortlist", body: str = "") -> str:
    heading_tag = f"<h1>{heading}</h1>" if heading else ""
    return (
        "<!DOCTYPE html><html><head><title>The 2026 Prize Shortlist</title></head>"
        f"<body><main>{heading_tag}{body}</main></body></html>"
    )


def _accept_html(entries: int = 4) -> str:
    body = "".join(
        _entry(f"Book {index}", AUTHORS[index - 1], f"{SOURCE_URL}/book-{index}") for index in range(1, entries + 1)
    )
    return _page(body=f"<ul>{body}</ul>")


def _verdict_kwargs(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "source_url": SOURCE_URL,
        "resolved_url": SOURCE_URL,
        "head_status": 200,
        "get_status": 200,
        "html": _accept_html(),
    }
    value.update(overrides)
    return value


def _sample_grounded(page_html: str, sample: EntrySample) -> None:
    assert sample.title in page_html
    assert sample.author in page_html
    assert sample.url in page_html


def test_clean_acceptance_exposes_structured_fields() -> None:
    verdict = evaluate_captured_source_evidence(_verdict_kwargs())
    assert verdict.accepted
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.reason_code == "ok"
    assert verdict.use_get_fallback is False
    assert verdict.detected_heading == "The 2026 Prize Shortlist"
    assert verdict.bounded_entry_count == 4
    assert "accepted" in verdict.reason


def test_acceptance_samples_are_grounded_capped_and_ordered() -> None:
    html = _accept_html(entries=6)
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=html))
    assert verdict.bounded_entry_count == 6
    assert len(verdict.samples) == 3
    assert [sample.title for sample in verdict.samples] == ["Book 1", "Book 2", "Book 3"]
    assert [sample.author for sample in verdict.samples] == ["Jane Author", "John Writer", "Ann Editor"]
    assert [sample.url for sample in verdict.samples] == [f"{SOURCE_URL}/book-{index}" for index in (1, 2, 3)]
    for sample in verdict.samples:
        _sample_grounded(html, sample)


def test_acceptance_fixture_json_grounds_realistic_list() -> None:
    evidence = json.loads((FIXTURES / "accept-list.json").read_text(encoding="utf-8"))
    verdict = evaluate_captured_source_evidence(evidence)
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.detected_heading == "The Prize announces 2025 shortlist"
    assert verdict.bounded_entry_count == 5
    first = verdict.samples[0]
    assert first.title == "The Revolutionists: The Story of the Extremists Who Hijacked the 1970s"
    assert first.author == "Jason Burke"
    assert first.url == "https://example.test/books-and-authors/the-revolutionists"
    _sample_grounded(evidence["html"], first)


def test_head_405_and_501_permit_single_plain_get_fallback() -> None:
    for status in (405, 501):
        verdict = evaluate_captured_source_evidence(_verdict_kwargs(head_status=status))
        assert verdict.accepted
        assert verdict.outcome == OUTCOME_ACCEPT
        assert verdict.use_get_fallback is True
        assert head_fallback_allowed(status)


def test_named_post_http_evidence_does_not_fabricate_head_status() -> None:
    verdict, fallback = validate_http_status_evidence(HttpStatusEvidence.declared_post(body_status=200))

    assert verdict is None
    assert fallback is False


def test_declared_post_http_errors_name_post_without_fabricating_head() -> None:
    verdict, fallback = validate_http_status_evidence(HttpStatusEvidence.declared_post(body_status=503))

    assert verdict is not None
    assert "POST must return 200" in verdict.reason
    assert fallback is False


def test_head_403_429_and_other_statuses_never_fall_back() -> None:
    for status in (403, 429, 404, 500, 502, 503):
        verdict = evaluate_captured_source_evidence(_verdict_kwargs(head_status=status, get_status=None, html=""))
        assert not verdict.accepted
        assert verdict.outcome == OUTCOME_REJECT
        assert verdict.reason_code == "head_denied"
        assert verdict.use_get_fallback is False
        assert f"HEAD {status}" in verdict.reason
        assert not head_fallback_allowed(status)


def test_get_http_failure_rejects() -> None:
    for status in (404, 500, 503):
        verdict = evaluate_captured_source_evidence(_verdict_kwargs(get_status=status))
        assert not verdict.accepted
        assert verdict.outcome == OUTCOME_REJECT
        assert verdict.reason_code == "http_error"
        assert f"got {status}" in verdict.reason


def test_missing_get_evidence_rejects() -> None:
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(get_status=None))
    assert not verdict.accepted
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "http_error"
    assert "missing" in verdict.reason


def test_empty_response_rejects() -> None:
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html="   "))
    assert not verdict.accepted
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "empty_response"


def test_bot_wall_rejects() -> None:
    captcha = _page(body="<p>verify you are human</p>") + '<div class="h-captcha"></div>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=captcha))
    assert not verdict.accepted
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "blocked_response"
    assert "CAPTCHA" in verdict.reason
    denied = _page(body="<p>Access Denied</p>")
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=denied))
    assert verdict.reason_code == "blocked_response"


def test_bare_captcha_heading_rejects_fail_closed() -> None:
    # A bare visible CAPTCHA heading with no challenge verb nearby is a
    # challenge wall even though the shared likely_blocked_response heuristic
    # does not flag it; the generic evaluator must fail closed on it.
    bare = _page(heading="CAPTCHA", body="<p>Please wait while we check your browser.</p>")
    assert not likely_blocked_response(bare)
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=bare))
    assert not verdict.accepted
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "blocked_response"
    assert "CAPTCHA" in verdict.reason


def test_verify_human_phrase_rejects_without_captcha_word() -> None:
    wall = _page(body="<p>Please verify you are human to continue.</p>")
    assert not likely_blocked_response(wall)
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=wall))
    assert verdict.reason_code == "blocked_response"


def test_unrelated_redirect_rejects() -> None:
    other_host = _verdict_kwargs(resolved_url="https://other.test/shortlist")
    verdict = evaluate_captured_source_evidence(other_host)
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "unrelated_redirect"
    home = _verdict_kwargs(resolved_url="https://example.test/")
    verdict = evaluate_captured_source_evidence(home)
    assert verdict.reason_code == "unrelated_redirect"


def test_same_origin_redirect_stays_acceptable() -> None:
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(resolved_url="https://example.test/shortlist/"))
    assert verdict.outcome == OUTCOME_ACCEPT


def test_unrelated_same_site_path_redirect_rejects() -> None:
    # A redirect to another path on the same site is a different page, not a
    # harmless trailing-slash normalization: it must reject as unrelated.
    verdict = evaluate_captured_source_evidence(
        _verdict_kwargs(
            source_url="https://example.test/nonfiction-shortlist",
            resolved_url="https://example.test/fiction-bestsellers",
        )
    )
    assert not verdict.accepted
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "unrelated_redirect"


def test_trailing_slash_variants_are_harmless_normalization() -> None:
    # Both trailing-slash directions are harmless normalization.
    for resolved in ("https://example.test/shortlist/", "https://example.test/shortlist"):
        verdict = evaluate_captured_source_evidence(
            _verdict_kwargs(source_url="https://example.test/shortlist", resolved_url=resolved)
        )
        assert verdict.outcome == OUTCOME_ACCEPT


def test_query_difference_rejects_as_unrelated_redirect() -> None:
    # A redirect to the same path with a different query is a different
    # page: /list?category=nonfiction to /list?category=fiction must reject
    # as unrelated, exactly like a different path (Issue #334).
    verdict = evaluate_captured_source_evidence(
        _verdict_kwargs(
            source_url="https://example.test/list?category=nonfiction",
            resolved_url="https://example.test/list?category=fiction",
        )
    )
    assert not verdict.accepted
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "unrelated_redirect"


def test_same_query_and_query_trailing_slash_stay_acceptable() -> None:
    # The same query on the resolved URL stays acceptable, and a trailing
    # slash on the path is still harmless normalization.
    for resolved in (
        "https://example.test/list?category=nonfiction",
        "https://example.test/list/?category=nonfiction",
    ):
        verdict = evaluate_captured_source_evidence(
            _verdict_kwargs(
                source_url="https://example.test/list?category=nonfiction",
                resolved_url=resolved,
            )
        )
        assert verdict.outcome == OUTCOME_ACCEPT
        assert verdict.reason_code == "ok"


def test_redirect_adds_query_rejects_as_unrelated() -> None:
    # A redirect that introduces a query where none was requested is a
    # different page, not harmless normalization.
    verdict = evaluate_captured_source_evidence(
        _verdict_kwargs(
            source_url="https://example.test/shortlist",
            resolved_url="https://example.test/shortlist?utm_source=newsletter",
        )
    )
    assert not verdict.accepted
    assert verdict.reason_code == "unrelated_redirect"


def test_parser_metadata_marks_navigation_anchors() -> None:
    # Anchors inside <nav> and inside an element with role="navigation" are
    # marked as navigation; other anchors are not.
    html = (
        '<nav><ul><li><a href="https://example.test/home">Home</a></li></ul></nav>'
        '<div role="navigation"><a href="https://example.test/books">Books</a></div>'
        '<ul><li><a href="https://example.test/shortlist/book-1">Book 1</a></li></ul>'
    )
    entries = server_rendered_list_entries(html, SOURCE_URL)
    assert [(entry.title, entry.block_tag, entry.in_nav) for entry in entries] == [
        ("Home", "li", True),
        ("Books", "", True),
        ("Book 1", "li", False),
    ]


def test_parser_metadata_marks_header_and_footer_anchors() -> None:
    # Anchors inside <header> and <footer> (and their ARIA roles) are marked
    # as header/footer landmarks; other anchors are not.
    html = (
        '<header><ul><li><a href="https://example.test/home">Home</a></li></ul></header>'
        '<div role="banner"><a href="https://example.test/sections">Sections</a></div>'
        '<footer><a href="https://example.test/about">About</a></footer>'
        '<div role="contentinfo"><a href="https://example.test/legal">Legal</a></div>'
        '<ul><li><a href="https://example.test/shortlist/book-1">Book 1</a></li></ul>'
    )
    entries = server_rendered_list_entries(html, SOURCE_URL)
    assert [(entry.title, entry.block_tag, entry.in_nav, entry.in_header, entry.in_footer) for entry in entries] == [
        ("Home", "li", False, True, False),
        ("Sections", "", False, True, False),
        ("About", "", False, False, True),
        ("Legal", "", False, False, True),
        ("Book 1", "li", False, False, False),
    ]


def test_nested_divs_do_not_end_navigation_landmark_prematurely() -> None:
    # A robust landmark stack: nested divs inside role="navigation" must not
    # prematurely end the landmark, so a <div> inside the landmark closing
    # before the landmark element itself must not unmark later anchors.
    html = (
        '<div role="navigation"><div><div><a href="https://example.test/books">Books</a></div></div></div>'
        '<nav><header><a href="https://example.test/header-link">Header link</a></header>'
        '<a href="https://example.test/menu">Menu</a></nav>'
        '<ul><li><a href="https://example.test/shortlist/book-1">Book 1</a></li></ul>'
    )
    entries = server_rendered_list_entries(html, SOURCE_URL)
    assert [(entry.title, entry.in_nav, entry.in_header, entry.in_footer) for entry in entries] == [
        ("Books", True, False, False),
        ("Header link", True, True, False),
        ("Menu", True, False, False),
        ("Book 1", False, False, False),
    ]


def test_navigation_plus_valid_bounded_list_accepts() -> None:
    # Site chrome in a <nav> must not count as book entries and must not
    # block a valid bounded book list.
    nav = '<nav><ul><li><a href="https://example.test/home">Home</a></li><li><a href="https://example.test/about">About</a></li></ul></nav>'
    body = "".join(_entry(f"Book {index}", AUTHORS[index - 1], f"{SOURCE_URL}/book-{index}") for index in (1, 2))
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=nav + f"<ul>{body}</ul>")))
    assert verdict.accepted
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.reason_code == "ok"
    assert verdict.bounded_entry_count == 2
    assert [sample.title for sample in verdict.samples] == ["Book 1", "Book 2"]


def test_navigation_links_do_not_cause_missing_author_rejection() -> None:
    # Unmarked navigation links carry no author; they must never reach the
    # per-entry author check. A bounded list with authors still accepts, and
    # a navigation-only page rejects as unlinked rather than missing_authors.
    nav = '<nav><ul><li><a href="https://example.test/home">Home</a></li></ul></nav>'
    body = _entry("Book 1", "Jane Author", f"{SOURCE_URL}/book-1")
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=nav + f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.bounded_entry_count == 1
    nav_only = _verdict_kwargs(html=_page(body=nav))
    verdict = evaluate_captured_source_evidence(nav_only)
    assert not verdict.accepted
    assert verdict.reason_code == "unlinked_entries"
    assert "missing_authors" not in verdict.reason


def test_header_and_footer_menus_do_not_block_valid_list() -> None:
    # Unmarked header/footer menus are site chrome: they must not count as
    # book entries and must not block a valid bounded book list.
    header = '<header><nav><ul><li><a href="https://example.test/home">Home</a></li></ul></nav></header>'
    footer = '<footer><ul><li><a href="https://example.test/privacy">Privacy</a></li></ul></footer>'
    body = "".join(_entry(f"Book {index}", AUTHORS[index - 1], f"{SOURCE_URL}/book-{index}") for index in (1, 2))
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=header + footer + f"<ul>{body}</ul>")))
    assert verdict.accepted
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.reason_code == "ok"
    assert verdict.bounded_entry_count == 2
    assert [sample.title for sample in verdict.samples] == ["Book 1", "Book 2"]


def test_header_only_page_rejects_as_unlinked() -> None:
    # A page whose only server-rendered anchors sit in a header menu is not
    # a book list: it rejects as unlinked, never as missing_authors.
    header = '<header><ul><li><a href="https://example.test/home">Home</a></li><li><a href="https://example.test/books">Books</a></li></ul></header>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=header)))
    assert not verdict.accepted
    assert verdict.reason_code == "unlinked_entries"
    assert "missing_authors" not in verdict.reason


def test_js_only_page_with_server_rendered_navigation_defers() -> None:
    # A JS-only page whose only server-rendered anchors are navigation links
    # must defer, not accept the navigation as book entries.
    script = '<script id="__NEXT_DATA__">{"props":{}}</script>'
    nav = '<nav><ul><li><a href="https://example.test/home">Home</a></li><li><a href="https://example.test/books">Books</a></li></ul></nav>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=script + nav)))
    assert not verdict.accepted
    assert verdict.outcome == OUTCOME_DEFER
    assert verdict.reason_code == "js_only"
    assert verdict.bounded_entry_count == 0


def test_narrative_prose_is_not_a_primary_author() -> None:
    # Arbitrary narrative prose must never be read as a primary author: the
    # fallback heuristic deterministically rejects narrative/list-label
    # terms and connector words.
    body = '<li><a href="https://example.test/shortlist/winner">The Annual Prize</a>Winner of the annual prize</li>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert not verdict.accepted
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "missing_authors"
    assert verdict.samples == ()
    assert "The Annual Prize" in verdict.reason


def test_sweeping_history_prose_is_not_an_invented_author() -> None:
    # The exact prose regression: "A sweeping history of Europe" must fail
    # closed. The fallback author candidate is a contiguous span of the
    # captured entry context, so the old word-deleting join that invented the
    # non-contiguous author "sweeping history Europe" cannot happen.
    body = '<li><a href="https://example.test/shortlist/book-1">Book 1</a>A sweeping history of Europe</li>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert not verdict.accepted
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "missing_authors"
    assert verdict.samples == ()
    assert "Book 1" in verdict.reason


def test_sweeping_history_prose_is_not_an_author_in_parser() -> None:
    # The parser-level guarantee behind the fail-closed verdict: the span
    # heuristic must not produce "sweeping history Europe" (or any other
    # word-deleted reconstruction) from the prose.
    from autoscout.source_feasibility import _span_author

    assert _span_author("A sweeping history of Europe") == ""
    assert _span_author("Winner of the annual prize") == ""
    assert _span_author("Jane Author") == ""


def test_author_label_marker_is_accepted() -> None:
    # An explicit "Author:" marker is a primary-author marker like By/Von/Par.
    body = (
        '<li><a href="https://example.test/shortlist/book-1">Book 1</a>'
        '<span class="primary-author">Author: Jane Author</span></li>'
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.samples == (EntrySample(title="Book 1", author="Jane Author", url=f"{SOURCE_URL}/book-1"),)


def test_visible_page_identity_matches_explicit_direct_child_selector() -> None:
    html = (
        '<div class="pn-colunas-06x07"><div class="pn-colunas-corpo">'
        '<div class="pn-editoria"><div class="pn-editoria-cabeca">'
        '<div class="pn-editoria-titulo">Visible page title</div>'
        "</div></div></div></div>"
    )
    parsed = parse_visible_page_identity(
        html,
        (".pn-colunas-06x07 > .pn-colunas-corpo > .pn-editoria > .pn-editoria-cabeca > .pn-editoria-titulo"),
    )
    assert parsed.identity_texts == ("Visible page title",)
    assert parsed.failure_cause is None


def test_visible_page_identity_rejects_ambiguous_single_class_selector() -> None:
    html = "".join(f'<div class="pn-editoria-titulo">Title {index}</div>' for index in range(1, 7))
    assert parse_visible_page_identity(html, ".pn-editoria-titulo").failure_cause == "multiple_matches"


@pytest.mark.parametrize("selector", ["div.title", ".a .b", ".a + .b", "#title", ".a > span", ".a >"])
def test_visible_title_selector_rejects_unsupported_syntax(selector: str) -> None:
    with pytest.raises(ValueError, match="visible title selector"):
        validate_visible_title_selector(selector)


def test_visible_page_identity_accepts_single_class_selector_when_headings_are_absent() -> None:
    html = '<title>Stale title</title><div class="pn-editoria-titulo">Visible page title</div>'
    assert parse_visible_page_identity(html, ".pn-editoria-titulo").identity_texts == ("Visible page title",)


def test_publishnews_visible_selector_advances_missing_heading_to_unlinked() -> None:
    evidence = json.loads((FIXTURES / "publishnews-visible-title.json").read_text(encoding="utf-8"))
    without_option = evaluate_captured_source_evidence(evidence)
    assert without_option.reason_code == "missing_heading"

    with_option = evaluate_captured_source_evidence(
        evidence_from_mapping(
            {
                **evidence,
                "visible_title_selector": (
                    ".pn-colunas-06x07 > .pn-colunas-corpo > .pn-editoria > .pn-editoria-cabeca > .pn-editoria-titulo"
                ),
            }
        )
    )
    assert with_option.reason_code == "unlinked_entries"
    assert with_option.detected_heading == (
        "Lista Nielsen-PublishNews de Mais Vendidos de Não Ficção Trade de 03/08/2026 a 09/08/2026"
    )
    assert with_option.bounded_entry_count == 0


def test_publishnews_fixture_uses_bounded_unlinked_row_shape() -> None:
    evidence = json.loads((FIXTURES / "publishnews-visible-title.json").read_text(encoding="utf-8"))
    html = evidence["html"]
    assert html.count("pn-ranking-livro-nome") == 2
    assert "pn-ranking-livros-posicao" not in html
    assert html.count("pn-ranking-livro-") >= 6
    assert "Book One" in html and "Book Two" in html
    assert "Jane Author" in html and "John Writer" in html
    assert "Publisher One" in html and "Publisher Two" in html
    assert "href=" not in html


def test_visible_identity_inline_hidden_style_fails_closed_with_important() -> None:
    html = '<div class="pn-editoria-titulo" style="display: none !important">Hidden</div>'
    assert parse_visible_page_identity(html, ".pn-editoria-titulo").failure_cause == "hidden"


@pytest.mark.parametrize("style", ["display:none!important", "visibility:hidden!important"])
def test_visible_identity_inline_hidden_style_without_spacing_fails_closed(style: str) -> None:
    html = f'<div class="pn-editoria-titulo" style="{style}">Hidden</div>'
    assert parse_visible_page_identity(html, ".pn-editoria-titulo").failure_cause == "hidden"


@pytest.mark.parametrize(
    ("html", "failure_cause"),
    (
        (
            '<div class="pn-editoria-titulo">First</div><div class="pn-editoria-titulo">Second</div>',
            "multiple_matches",
        ),
        ('<div class="pn-editoria-titulo"></div>', "empty"),
        ('<div hidden><div class="pn-editoria-titulo">Hidden</div></div>', "hidden"),
        ('<div aria-hidden="true"><div class="pn-editoria-titulo">Hidden</div></div>', "hidden"),
        # A raw-text element's content is never parsed as markup, so the
        # selector has nothing to match rather than a hidden match.
        ('<script><div class="pn-editoria-titulo">Hidden</div></script>', "no_match"),
        ('<style><div class="pn-editoria-titulo">Hidden</div></style>', "no_match"),
        ('<template><div class="pn-editoria-titulo">Hidden</div></template>', "hidden"),
        ('<noscript><div class="pn-editoria-titulo">Hidden</div></noscript>', "hidden"),
        ('<nav><div class="pn-editoria-titulo">Chrome</div></nav>', "chrome"),
        ('<header><div class="pn-editoria-titulo">Chrome</div></header>', "chrome"),
        ('<footer><div class="pn-editoria-titulo">Chrome</div></footer>', "chrome"),
        ('<div role="navigation"><div class="pn-editoria-titulo">Chrome</div></div>', "chrome"),
        ('<div role="banner"><div class="pn-editoria-titulo">Chrome</div></div>', "chrome"),
        ('<div role="contentinfo"><div class="pn-editoria-titulo">Chrome</div></div>', "chrome"),
        # The match itself is visible; all of its text is hidden.
        (
            '<div class="pn-editoria-titulo"><span style="visibility:hidden">Hidden</span></div>',
            "empty",
        ),
    ),
)
def test_visible_identity_selector_fails_closed_with_a_specific_cause(html: str, failure_cause: str) -> None:
    parsed = parse_visible_page_identity(html, ".pn-editoria-titulo")
    assert parsed.identity_texts == ()
    assert parsed.failure_cause == failure_cause


def test_visible_identity_allows_entry_header_but_not_page_chrome() -> None:
    html = '<article><header><div class="pn-editoria-titulo">Entry title</div></header></article>'
    assert parse_visible_page_identity(html, ".pn-editoria-titulo").identity_texts == ("Entry title",)


def test_visible_identity_rejects_header_inside_page_level_section() -> None:
    html = '<section><header><div class="pn-editoria-titulo">Chrome title</div></header></section>'
    assert parse_visible_page_identity(html, ".pn-editoria-titulo").failure_cause == "chrome"


def test_visible_page_identity_uses_declared_selector_before_h1_h6_and_ignores_title() -> None:
    html = '<title>Stale title</title><h2>Real heading</h2><div class="pn-editoria-titulo">Other</div>'
    assert parse_visible_page_identity(html, ".pn-editoria-titulo").identity_texts == ("Other",)
    assert parse_visible_page_identity(html).identity_texts == ("Real heading",)
    assert parse_visible_page_identity("<title>Stale title</title>") == VisiblePageIdentityResult()


@pytest.mark.parametrize(
    ("html", "failure_cause", "expected_reason"),
    (
        (
            '<title>Stale title</title><div class="other">Not the title</div>',
            "no_match",
            "visible title selector matched no element",
        ),
        (
            '<title>Stale title</title><img class="title" alt="Void match">',
            "no_match",
            "visible title selector matched no element",
        ),
        (
            '<div class="title">First</div><div class="title">Second</div>',
            "multiple_matches",
            "visible title selector matched more than one element",
        ),
        (
            '<img class="title" alt="Void match"><div class="title">Normal</div>',
            "multiple_matches",
            "visible title selector matched more than one element",
        ),
        (
            '<div hidden><div class="title">Hidden</div></div>',
            "hidden",
            "visible title selector matched a hidden element",
        ),
        (
            '<nav><div class="title">Chrome</div></nav>',
            "chrome",
            "visible title selector matched an element inside page-level chrome",
        ),
        (
            '<div class="title"> </div>',
            "empty",
            "visible title selector matched an element with no text",
        ),
    ),
)
def test_visible_title_selector_failure_cause_reaches_evaluator(
    html: str, failure_cause: str, expected_reason: str
) -> None:
    parsed = parse_visible_page_identity(html, ".title")
    assert parsed.identity_texts == ()
    assert parsed.failure_cause == failure_cause

    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=html, visible_title_selector=".title"))
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == REASON_VISIBLE_TITLE_SELECTOR
    assert verdict.reason == expected_reason
    assert verdict.detected_heading == ""


def test_invalid_visible_title_selector_propagates_even_with_heading() -> None:
    with pytest.raises(ValueError, match="visible title selector"):
        evaluate_captured_source_evidence(_verdict_kwargs(html=_page(), visible_title_selector="invalid class"))


def test_missing_heading_rejects_without_selector() -> None:
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(heading="", body="<ul></ul>")))
    assert verdict == FeasibilityVerdict(
        accepted=False,
        reason="GET page has no h1-h6 heading",
        use_get_fallback=False,
        outcome=OUTCOME_REJECT,
        reason_code="missing_heading",
        detected_heading="",
        bounded_entry_count=0,
        samples=(),
    )


def test_h1_without_selector_preserves_complete_verdict() -> None:
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body="<ul></ul>")))
    assert verdict == FeasibilityVerdict(
        accepted=False,
        reason="page exposes no server-rendered linked list entries",
        use_get_fallback=False,
        outcome=OUTCOME_REJECT,
        reason_code="unlinked_entries",
        detected_heading="The 2026 Prize Shortlist",
        bounded_entry_count=0,
        samples=(),
    )


def test_js_only_page_defers_with_structured_fields() -> None:
    evidence = json.loads((FIXTURES / "js-only-page.json").read_text(encoding="utf-8"))
    verdict = evaluate_captured_source_evidence(evidence)
    assert not verdict.accepted
    assert verdict.outcome == OUTCOME_DEFER
    assert verdict.reason_code == "js_only"
    assert verdict.detected_heading == "Nonfiction Booklist"
    assert verdict.bounded_entry_count == 0
    assert verdict.samples == ()
    assert "JavaScript" in verdict.reason


def test_js_markers_with_server_rendered_list_still_accept() -> None:
    script = '<script id="__NEXT_DATA__">{"props":{}}</script>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=script + _accept_html()))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.reason_code == "ok"


@pytest.mark.parametrize(
    ("fixture_name", "reason_code", "detected_heading", "bounded_entry_count", "reason_fragment"),
    (
        ("bot-wall.json", "blocked_response", "", 0, "CAPTCHA"),
        ("unrelated-redirect.json", "unrelated_redirect", "", 0, "unrelated target"),
        ("http-failure.json", "http_error", "", 0, "got 503"),
        ("missing-heading.json", "missing_heading", "", 0, "no h1-h6 heading"),
        ("unlinked-list.json", "unlinked_entries", "2026 Shortlist", 0, "no server-rendered linked list entries"),
        ("unbounded-list.json", "unbounded_entries", "2026 Shortlist", 0, "not inside a per-entry list container"),
        ("missing-author.json", "missing_authors", "2026 Shortlist", 1, "Book One"),
    ),
)
def test_rejection_fixtures_exercise_capture_evaluator(
    fixture_name: str,
    reason_code: str,
    detected_heading: str,
    bounded_entry_count: int,
    reason_fragment: str,
) -> None:
    evidence = json.loads((FIXTURES / fixture_name).read_text(encoding="utf-8"))
    verdict = evaluate_captured_source_evidence(evidence)
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == reason_code
    assert verdict.detected_heading == detected_heading
    assert verdict.bounded_entry_count == bounded_entry_count
    assert verdict.samples == ()
    assert reason_fragment in verdict.reason


def test_unlinked_list_rejects() -> None:
    body = "".join(_entry(f"Book {index}", AUTHORS[index - 1], "", linked=False) for index in range(1, 4))
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert not verdict.accepted
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "unlinked_entries"
    assert verdict.bounded_entry_count == 0


def test_unbounded_flat_links_reject() -> None:
    body = "".join(f'<a href="{SOURCE_URL}/book-{index}">Book {index}</a>' for index in range(1, 4))
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<p>{body}</p>")))
    assert not verdict.accepted
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "unbounded_entries"
    assert verdict.bounded_entry_count == 0


def test_direct_anchor_section_wrapper_rejects_as_unbounded() -> None:
    # A page-level <section> wrapping several direct anchors is a container,
    # not one bounded entry per book: without per-entry blocks it must reject
    # as unbounded rather than count each direct anchor as a bounded entry.
    body = "".join(f'<a href="{SOURCE_URL}/book-{index}">Book {index}</a>' for index in range(1, 4))
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<section>{body}</section>")))
    assert not verdict.accepted
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "unbounded_entries"
    assert verdict.bounded_entry_count == 0


def test_nested_section_with_per_entry_list_accepts() -> None:
    # A section that wraps real per-entry containers (ul > li) still accepts:
    # each anchor reports its innermost enclosing entry block (li).
    body = "".join(_entry(f"Book {index}", AUTHORS[index - 1], f"{SOURCE_URL}/book-{index}") for index in range(1, 4))
    verdict = evaluate_captured_source_evidence(
        _verdict_kwargs(html=_page(body=f"<section><h2>Shortlist</h2><ul>{body}</ul></section>"))
    )
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.bounded_entry_count == 3


def test_missing_author_rejects_with_offending_title() -> None:
    body = "".join(_entry(f"Book {index}", AUTHORS[index - 1], f"{SOURCE_URL}/book-{index}") for index in range(1, 4))
    body += _entry("Book Four", "", f"{SOURCE_URL}/book-4")
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert not verdict.accepted
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "missing_authors"
    assert "Book Four" in verdict.reason
    assert verdict.bounded_entry_count == 4
    assert verdict.samples == ()


def test_author_marker_form_is_accepted() -> None:
    body = "".join(
        f'<li><span class="rank">{index}</span>'
        f'<a href="{SOURCE_URL}/book-{index}">Book {index}</a>'
        f'<span class="primary-author">By: {AUTHORS[index - 1]}</span></li>'
        for index in range(1, 4)
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ol>{body}</ol>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert [sample.author for sample in verdict.samples] == ["Jane Author", "John Writer", "Ann Editor"]


def test_two_entry_bounded_list_accepts() -> None:
    # The issue specifies a bounded qualifying list with no minimum count:
    # a valid two-entry bounded list must accept, not reject on a threshold.
    body = "".join(_entry(f"Book {index}", AUTHORS[index - 1], f"{SOURCE_URL}/book-{index}") for index in (1, 2))
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.accepted
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.reason_code == "ok"
    assert verdict.bounded_entry_count == 2
    assert [sample.title for sample in verdict.samples] == ["Book 1", "Book 2"]


def test_title_link_plus_linked_author_is_one_book_entry() -> None:
    # A book title link plus a linked author inside the same li is ONE book
    # entry, not two: parser metadata classifies the author link separately,
    # so the title remains the primary book link and the grounded author
    # evidence supplies the sample author.
    body = (
        '<li><a href="https://example.test/shortlist/book-1">Book 1</a>'
        '<a class="author" href="https://example.test/authors/jane-author">Jane Author</a></li>'
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.reason_code == "ok"
    assert verdict.bounded_entry_count == 1
    assert verdict.samples == (EntrySample(title="Book 1", author="Jane Author", url=f"{SOURCE_URL}/book-1"),)
    _sample_grounded(_page(body=f"<ul>{body}</ul>"), verdict.samples[0])


def test_bare_linked_author_plus_title_is_one_book_entry() -> None:
    # The same grouping holds for a bare linked author with no author
    # attribute: parser URL metadata classifies the author link separately,
    # while the title link qualifies as the primary link and the inferred
    # author evidence grounds the sample author.
    body = (
        '<li><a href="https://example.test/shortlist/book-1">Book 1</a>'
        '<a href="https://example.test/authors/jane-author">Jane Author</a></li>'
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.bounded_entry_count == 1
    assert verdict.samples == (EntrySample(title="Book 1", author="Jane Author", url=f"{SOURCE_URL}/book-1"),)


def test_buy_now_link_is_not_a_second_book_entry() -> None:
    # A "Buy now" commerce link inside the same li is not a second book:
    # parser metadata classifies the action link as non-book, so primary-link
    # selection ignores it and parses the grounded author evidence separately.
    body = (
        '<li><a href="https://example.test/shortlist/book-1">Book 1</a>'
        '<a href="https://example.test/buy/book-1">Buy now</a>'
        '<span class="author">Jane Author</span></li>'
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.reason_code == "ok"
    assert verdict.bounded_entry_count == 1
    assert verdict.samples == (EntrySample(title="Book 1", author="Jane Author", url=f"{SOURCE_URL}/book-1"),)


def test_read_more_and_add_to_cart_links_are_not_book_entries() -> None:
    # More deterministic action labels behave like "Buy now": parser
    # metadata classifies them as non-book links, so they never become book
    # entries and the grounded author evidence remains separate.
    body = (
        '<li><a href="https://example.test/shortlist/book-1">Book 1</a>'
        '<a href="https://example.test/cart/book-1">Add to cart</a>'
        '<a href="https://example.test/excerpt/book-1">Read more</a>'
        '<a href="https://example.test/cover/book-1">View cover image</a>'
        '<span class="author">Jane Author</span></li>'
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.bounded_entry_count == 1
    assert verdict.samples == (EntrySample(title="Book 1", author="Jane Author", url=f"{SOURCE_URL}/book-1"),)


def test_action_only_group_is_not_an_accepted_book_entry() -> None:
    # A bounded container that holds only action links is not a book entry:
    # it neither becomes a second book nor blocks the rest of the page.
    body = (
        '<li><a href="https://example.test/buy/book-1">Buy now</a></li>'
        '<li><a href="https://example.test/shortlist/book-1">Book 1</a><span class="author">Jane Author</span></li>'
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.bounded_entry_count == 1
    assert verdict.samples == (EntrySample(title="Book 1", author="Jane Author", url=f"{SOURCE_URL}/book-1"),)


def test_author_only_group_is_not_an_accepted_book_entry() -> None:
    # A bounded container that holds only an explicit author link is not a
    # book entry and never becomes an accepted book sample.
    body = (
        '<li><a class="author" href="https://example.test/authors/jane-author">Jane Author</a></li>'
        '<li><a href="https://example.test/shortlist/book-1">Book 1</a><span class="author">Jane Author</span></li>'
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.bounded_entry_count == 1
    assert verdict.samples == (EntrySample(title="Book 1", author="Jane Author", url=f"{SOURCE_URL}/book-1"),)


def test_author_only_page_rejects_as_unlinked() -> None:
    # A page whose only bounded links are author links exposes no book-level
    # link at all: it rejects fail-closed instead of accepting the author
    # name as a book title.
    body = '<li><a class="author" href="https://example.test/authors/jane-author">Jane Author</a></li>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert not verdict.accepted
    assert verdict.reason_code == "unlinked_entries"
    assert verdict.samples == ()


def test_samples_grounded_in_same_captured_entry() -> None:
    # Accepted samples carry title, author, and URL all grounded in the same
    # captured entry: the title and URL come from the primary book-level
    # link, the author from the visible text of the same bounded container.
    html = _page(
        body=(
            "<ul>"
            '<li><a href="https://example.test/shortlist/book-1">Book 1</a>'
            '<a class="author" href="https://example.test/authors/jane-author">Jane Author</a>'
            '<a href="https://example.test/buy/book-1">Buy now</a></li>'
            '<li><a href="https://example.test/shortlist/book-2">Book 2</a>'
            '<span class="author">John Writer</span></li>'
            "</ul>"
        )
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=html))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.bounded_entry_count == 2
    assert verdict.samples == (
        EntrySample(title="Book 1", author="Jane Author", url=f"{SOURCE_URL}/book-1"),
        EntrySample(title="Book 2", author="John Writer", url=f"{SOURCE_URL}/book-2"),
    )
    for sample in verdict.samples:
        _sample_grounded(html, sample)


def test_mapping_adapter_accepts_aliases_and_ignores_unknown_keys() -> None:
    evidence = evidence_from_mapping(
        {
            "url": SOURCE_URL,
            "final_url": SOURCE_URL,
            "head": 200,
            "status": 200,
            "html_content": _accept_html(),
            "future_probe_field": "ignored",
        }
    )
    assert evidence.source_url == SOURCE_URL
    assert evidence.resolved_url == SOURCE_URL
    assert evidence.head_status == 200
    assert evidence.get_status == 200
    assert evaluate_captured_source_evidence(evidence).outcome == OUTCOME_ACCEPT
    assert evaluate_captured_source_evidence(evidence_from_mapping(evidence.__dict__)).outcome == OUTCOME_ACCEPT


def test_mapping_adapter_explicit_null_get_status_is_missing_evidence() -> None:
    mapped = evidence_from_mapping({"url": SOURCE_URL, "html": _accept_html(), "head_status": 200, "get_status": None})
    assert mapped.get_status is None
    assert evaluate_captured_source_evidence(mapped).reason_code == "http_error"
    assert "GET evidence is missing" in evaluate_captured_source_evidence(mapped).reason
    # There is no implicit 200: an absent get-status key is missing evidence
    # exactly like an explicit null, and a body-only capture rejects.
    defaulted = evidence_from_mapping({"url": SOURCE_URL, "html": _accept_html()})
    assert defaulted.get_status is None
    assert defaulted.head_status is None
    assert evaluate_captured_source_evidence(defaulted).outcome == OUTCOME_REJECT


def test_body_only_mapping_rejects_before_parsing() -> None:
    # A capture that carries the body but no HEAD/GET status evidence must
    # reject before any page parsing: missing status evidence is never
    # success, and absent HEAD evidence is not allowed.
    body_only = evidence_from_mapping({"url": SOURCE_URL, "html": _accept_html()})
    assert body_only.head_status is None and body_only.get_status is None
    verdict = evaluate_captured_source_evidence(body_only)
    assert not verdict.accepted
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "head_denied"
    assert "HEAD evidence is missing" in verdict.reason
    assert verdict.detected_heading == ""
    assert verdict.samples == ()


def test_missing_head_evidence_rejects() -> None:
    # Explicit GET 200 evidence does not excuse a missing HEAD record.
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(head_status=None))
    assert not verdict.accepted
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "head_denied"
    assert "HEAD evidence is missing" in verdict.reason


def test_mapping_adapter_accepts_captured_page_source_page_key() -> None:
    # Existing repository page captures carry source_page as the captured
    # page URL; the pure evaluator must not read it as an unrelated redirect.
    evidence = evidence_from_mapping(
        {
            "source_name": "Example Shortlist",
            "source_page": SOURCE_URL,
            "resolved_url": SOURCE_URL,
            "html": _accept_html(),
            "head_status": 200,
            "get_status": 200,
        }
    )
    assert evidence.source_url == SOURCE_URL
    verdict = evaluate_captured_source_evidence(evidence)
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.reason_code == "ok"


def test_mapping_adapter_explicit_source_url_beats_source_page() -> None:
    evidence = evidence_from_mapping(
        {
            "source_url": SOURCE_URL,
            "url": "https://alias.test/ignored",
            "source_page": "https://page.test/ignored",
            "head_status": 200,
            "get_status": 200,
            "html": _accept_html(),
        }
    )
    assert evidence.source_url == SOURCE_URL
    assert evaluate_captured_source_evidence(evidence).outcome == OUTCOME_ACCEPT


def test_verdict_dataclass_stays_positionally_backward_compatible() -> None:
    rejected = FeasibilityVerdict(False, "some reason")
    assert rejected.accepted is False
    assert rejected.reason == "some reason"
    assert rejected.use_get_fallback is False
    # Legacy positional constructions keep the documented defaults, and the
    # dataclass invariant derives the outcome from the accepted flag so the
    # structured fields stay consistent: accepted == (outcome == accept).
    assert rejected.outcome == OUTCOME_REJECT
    assert rejected.reason_code == ""
    assert rejected.detected_heading == ""
    assert rejected.bounded_entry_count == 0
    assert rejected.samples == ()
    accepted = FeasibilityVerdict(True, "ok reason", True)
    assert accepted.accepted and accepted.use_get_fallback
    assert accepted.outcome == OUTCOME_ACCEPT
    assert accepted.reason_code == ""
    deferred = FeasibilityVerdict(False, "defer reason", False, OUTCOME_DEFER)
    assert not deferred.accepted
    assert deferred.outcome == OUTCOME_DEFER


def test_verdict_invariant_rejects_explicit_contradiction() -> None:
    # The outcome is the structured source of truth: an explicit outcome that
    # contradicts the accepted flag is a bug and must not silently produce an
    # inconsistent verdict.
    with pytest.raises(ValueError, match="contradictory verdict"):
        FeasibilityVerdict(True, "ok reason", True, OUTCOME_REJECT)
    with pytest.raises(ValueError, match="contradictory verdict"):
        FeasibilityVerdict(False, "bad reason", False, OUTCOME_ACCEPT)


def test_audible_evaluator_keeps_legacy_fields_and_fills_new_ones() -> None:
    source = {"url": SOURCE_URL, "audible_explicit_chart": True, "expected_page_heading": "The 2026 Prize Shortlist"}
    verdict = evaluate_audible_chart_feasibility(
        source, head_status=403, get_status=None, resolved_url=SOURCE_URL, html=""
    )
    assert not verdict.accepted
    assert verdict.use_get_fallback is False
    assert verdict.reason == "HEAD 403 does not permit collection"
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "head_denied"


def test_unmarked_author_link_before_title_uses_book_link() -> None:
    # Issue #334 review: an unmarked author link that precedes the title link
    # must never become the primary book entry. The author-page URL is
    # deterministic parser evidence, so the sample keeps the grounded book
    # title and book URL while the linked author still grounds the author.
    body = (
        '<li><a href="https://example.test/authors/jane-author">Jane Author</a>'
        '<a href="https://example.test/books/quantum-worlds">Quantum Worlds</a></li>'
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.reason_code == "ok"
    assert verdict.bounded_entry_count == 1
    assert verdict.samples == (
        EntrySample(
            title="Quantum Worlds",
            author="Jane Author",
            url="https://example.test/books/quantum-worlds",
        ),
    )
    _sample_grounded(_page(body=f"<ul>{body}</ul>"), verdict.samples[0])


def test_parser_marks_unmarked_author_page_links() -> None:
    # Issue #334 review: the parser classifies an author-page URL as an
    # author link even without rel/itemprop/class metadata, so the policy
    # layer never picks it as the primary book-level link. A book page whose
    # path merely contains the word "author" is not an author page.
    html = (
        '<ul><li><a href="https://example.test/authors/jane-author">Jane Author</a>'
        '<a href="https://example.test/books/the-author">The Author</a></li></ul>'
    )
    entries = server_rendered_list_entries(html, SOURCE_URL)
    assert [(entry.title, entry.link_kind) for entry in entries] == [
        ("Jane Author", LINK_KIND_AUTHOR),
        ("The Author", "book"),
    ]


def test_read_write_own_title_is_not_an_action_label() -> None:
    # Issue #334 review: a valid title that merely starts with the
    # single-word action label "read" must not be classified as an action
    # link and dropped. Single-word labels match only exactly; multi-word
    # labels keep their prefix behavior.
    assert is_action_label("read")
    assert is_action_label("read more")
    assert is_action_label("read more - kindle")
    assert not is_action_label("Read Write Own")
    assert not is_action_label("Reading")
    body = (
        '<li><a href="https://example.test/books/read-write-own">Read Write Own</a>'
        '<span class="author">Jane Author</span></li>'
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.reason_code == "ok"
    assert verdict.bounded_entry_count == 1
    assert verdict.samples == (
        EntrySample(title="Read Write Own", author="Jane Author", url="https://example.test/books/read-write-own"),
    )


def test_share_link_does_not_hide_the_author() -> None:
    # Issue #334 review: parser metadata classifies a single-word action
    # anchor such as "Share" as non-book, so primary-link selection ignores
    # it. The grounded author evidence is parsed separately, never by
    # arbitrary single-word matching.
    body = (
        '<li><a href="https://example.test/books/q">Quantum Worlds</a>'
        '<a href="https://example.test/share/quantum-worlds">Share</a>'
        '<span class="author">Jane Author</span></li>'
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.reason_code == "ok"
    assert verdict.bounded_entry_count == 1
    assert verdict.samples == (
        EntrySample(title="Quantum Worlds", author="Jane Author", url="https://example.test/books/q"),
    )


def test_article_header_is_not_site_chrome() -> None:
    # Issue #334 review: a <header> (or its ARIA banner equivalent) nested
    # inside an actual per-entry container is that entry's own header, not
    # page-level site chrome, so
    # <article><header><a>…</a></header>…</article> must qualify.
    html = _page(
        body=(
            "<article><header>"
            '<a href="https://example.test/books/q">Quantum Worlds</a>'
            '</header><span class="author">Jane Author</span></article>'
            '<article><div role="banner">'
            '<a href="https://example.test/books/w">World of Wonders</a>'
            '</div><span class="author">John Writer</span></article>'
        )
    )
    entries = server_rendered_list_entries(html, SOURCE_URL)
    assert [(entry.title, entry.block_tag, entry.in_header) for entry in entries] == [
        ("Quantum Worlds", "article", False),
        ("World of Wonders", "article", False),
    ]
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=html))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.reason_code == "ok"
    assert verdict.bounded_entry_count == 2
    assert verdict.samples == (
        EntrySample(title="Quantum Worlds", author="Jane Author", url="https://example.test/books/q"),
        EntrySample(title="World of Wonders", author="John Writer", url="https://example.test/books/w"),
    )


def test_page_level_header_keeps_filtering_when_wrapping_the_list() -> None:
    # Issue #334 review: a page-level <header> that wraps the whole list
    # still opens before any per-entry container and keeps filtering as
    # site chrome.
    html = _page(
        body=(
            "<header><ul><li><a href='https://example.test/home'>Home</a></li></ul></header>"
            "<article><header>"
            '<a href="https://example.test/books/q">Quantum Worlds</a>'
            '</header><span class="author">Jane Author</span></article>'
        )
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=html))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.reason_code == "ok"
    assert verdict.bounded_entry_count == 1
    assert verdict.samples == (
        EntrySample(title="Quantum Worlds", author="Jane Author", url="https://example.test/books/q"),
    )


def test_powerful_exploration_prose_is_not_an_author() -> None:
    # Issue #334 review: descriptive prose with a lowercase non-particle
    # word must fail closed instead of becoming the invented author
    # "Powerful exploration across Europe".
    from autoscout.source_feasibility import _span_author

    assert _span_author("Powerful exploration across Europe") == ""
    body = '<li><a href="https://example.test/books/q">Quantum Worlds</a>Powerful exploration across Europe</li>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert not verdict.accepted
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "missing_authors"
    assert verdict.samples == ()
    assert "Quantum Worlds" in verdict.reason


def test_capitalization_alone_is_not_author_evidence() -> None:
    # Issue #334 review: capitalization alone is not valid author evidence.
    # Title-case prose fails closed even when every word is capitalized,
    # and an unmarked lowercase span is ambiguous prose, not a name: "bell
    # hooks" is accepted only through grounded author structure (an
    # author-marked element or an explicit marker).
    from autoscout.source_feasibility import _span_author

    assert _span_author("Powerful Exploration Across Europe") == ""
    assert _span_author("bell hooks") == ""
    assert _span_author("powerful exploration") == ""


def test_title_case_prose_is_not_an_author_in_evaluator() -> None:
    # Issue #334 review: the evaluator-level regression for the fully
    # capitalized prose variant: bare title-case prose after the title link
    # must reject fail-closed exactly like its lowercase form.
    body = '<li><a href="https://example.test/books/q">Quantum Worlds</a>Powerful Exploration Across Europe</li>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert not verdict.accepted
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "missing_authors"
    assert verdict.samples == ()
    assert "Quantum Worlds" in verdict.reason


def test_lowercase_author_name_is_accepted_in_evaluator() -> None:
    # Issue #334 review: a valid lowercase name is deterministic author
    # evidence only inside grounded author structure. The author-marked
    # element path accepts "bell hooks" without any capitalized word, while
    # the same text as bare unmarked prose is ambiguous ("powerful
    # exploration" is prose, "bell hooks" is a name) and fails closed.
    body = '<li><a href="https://example.test/books/q">Quantum Worlds</a><span class="author">bell hooks</span></li>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.reason_code == "ok"
    assert verdict.bounded_entry_count == 1
    assert verdict.samples == (
        EntrySample(title="Quantum Worlds", author="bell hooks", url="https://example.test/books/q"),
    )
    bare = '<li><a href="https://example.test/books/q">Quantum Worlds</a>bell hooks</li>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{bare}</ul>")))
    assert not verdict.accepted
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "missing_authors"
    assert verdict.samples == ()
    assert "Quantum Worlds" in verdict.reason
    lowercase_prose = '<li><a href="https://example.test/books/q">Quantum Worlds</a>powerful exploration</li>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{lowercase_prose}</ul>")))
    assert not verdict.accepted
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "missing_authors"
    assert verdict.samples == ()


def test_author_element_metadata_grounds_author_text() -> None:
    # Issue #334 review: visible grounded structure (an author-marked
    # element carrying class="author") is stronger author evidence than the
    # conservative pure-text fallback: the marked text wins even when
    # trailing prose would fail the whole context span closed.
    body = (
        '<li><a href="https://example.test/books/q">Quantum Worlds</a>'
        '<span class="author">bell hooks</span>Published 1999</li>'
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.reason_code == "ok"
    assert verdict.bounded_entry_count == 1
    assert verdict.samples == (
        EntrySample(title="Quantum Worlds", author="bell hooks", url="https://example.test/books/q"),
    )


def test_lukasz_orbitowski_name_is_preserved() -> None:
    # Issue #334 review: grounded author parsing is Unicode-safe, so
    # extended-Latin names keep their first character instead of becoming
    # "ukasz Orbitowski".
    from autoscout.source_feasibility import _grounded_author_span
    from autoscout.source_feasibility import _span_author

    assert _span_author("Łukasz Orbitowski") == ""
    assert _grounded_author_span("Łukasz Orbitowski") == "Łukasz Orbitowski"
    body = (
        '<li><a href="https://example.test/books/q">Quantum Worlds</a>'
        '<span class="author">Łukasz Orbitowski</span></li>'
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.reason_code == "ok"
    assert verdict.samples == (
        EntrySample(title="Quantum Worlds", author="Łukasz Orbitowski", url="https://example.test/books/q"),
    )


def test_span_author_keeps_russian_latin_and_particle_names() -> None:
    # Issue #334 review: grounded name-like validation preserves accented,
    # multilingual, and lowercase-particle names while prose fails closed.
    from autoscout.source_feasibility import _grounded_author_span
    from autoscout.source_feasibility import _span_author

    for author in (
        "Иван Иванов",
        "Анна Старобинец",
        "José Saramago",
        "Otto von Bismarck",
        "Jan van der Berg",
        "Мария фон Трапп",
    ):
        assert _span_author(author) == ""
        assert _grounded_author_span(author) == author
    assert _span_author("across Europe") == ""
    # Capitalization alone is never author evidence: the all-capitalized
    # three-word form is title-case prose, exactly like the four-word
    # variant, and a fully lowercase span is ambiguous prose (Issue #334
    # review).
    assert _span_author("Powerful Exploration Europe") == ""
    assert _span_author("Powerful Exploration Across Europe") == ""
    assert _span_author("powerful exploration") == ""
    # An unmarked lowercase span is ambiguous prose: lowercase names such
    # as "bell hooks" are accepted only through grounded author structure
    # (an author-marked element or an explicit marker).
    assert _span_author("bell hooks") == ""
    # Ordinary multiword names are accepted only when visibly grounded;
    # bare title-case spans of three or more words fail closed.
    assert _span_author("Mary Ann Evans") == ""
    assert _span_author("Gabriel Garcia Marquez") == ""


def test_profile_author_url_is_recognized() -> None:
    # Issue #334 review: a /profile/ author route is deterministic author-page
    # evidence exactly like /authors/, so the entry accepts with the book
    # title and book URL, never the profile URL. The identity slug grounds the
    # complete visible person display.
    body = (
        '<li><a href="https://example.test/profile/jane-doe">Jane Doe</a>'
        '<a href="https://example.test/books/q">Quantum Worlds</a></li>'
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.reason_code == "ok"
    assert verdict.bounded_entry_count == 1
    assert verdict.samples == (
        EntrySample(title="Quantum Worlds", author="Jane Doe", url="https://example.test/books/q"),
    )


def test_unrecognized_author_convention_rejects_as_ambiguous() -> None:
    # Issue #334 review: when an entry still holds several book-kind anchors
    # with distinct URLs (an unrecognized author route next to the title
    # link), no deterministic primary book link exists. The entry rejects
    # fail-closed instead of accepting the author-page URL as the book
    # sample.
    body = (
        '<li><a href="https://example.test/jane-author">Jane Author</a>'
        '<a href="https://example.test/books/q">Quantum Worlds</a></li>'
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert not verdict.accepted
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == REASON_ENTRY_SHAPE
    assert verdict.samples == ()
    assert "multiple distinct book-level links" in verdict.reason


def test_no_deterministic_primary_title_rejects_with_entry_shape_reason() -> None:
    # A same-URL group with an unmatched relational label has no deterministic
    # title. This is an entry-shape ambiguity, not missing author evidence.
    body = (
        '<li><a href="https://example.test/books/q">Image of Missing Title</a>'
        '<a href="https://example.test/books/q">Quantum Worlds</a></li>'
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert not verdict.accepted
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == REASON_ENTRY_SHAPE
    assert verdict.samples == ()
    assert "no deterministic primary title" in verdict.reason


def test_duplicate_cover_and_title_links_share_one_book_url() -> None:
    # Issue #334 review: duplicate anchors that point at the same book URL
    # (a cover link plus a title link) are one book link, not an ambiguous
    # entry. Every same-URL anchor record is preserved (the pending store is
    # never keyed by href), and the same-URL group is selected as one book.
    # The explicit author element is parsed as grounded evidence, so the
    # accepted author is exactly "Jane Author" - never a title-text echo.
    body = (
        '<li><a href="https://example.test/books/q">Quantum Worlds</a>'
        '<a href="https://example.test/books/q">Quantum Worlds</a>'
        '<span class="author">Jane Author</span></li>'
    )
    html = _page(body=f"<ul>{body}</ul>")
    entries = server_rendered_list_entries(html, SOURCE_URL)
    assert [(entry.title, entry.url) for entry in entries] == [
        ("Quantum Worlds", "https://example.test/books/q"),
        ("Quantum Worlds", "https://example.test/books/q"),
    ]
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=html))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.reason_code == "ok"
    assert verdict.bounded_entry_count == 1
    assert verdict.samples == (
        EntrySample(title="Quantum Worlds", author="Jane Author", url="https://example.test/books/q"),
    )


def test_cover_link_then_title_link_selects_the_real_title() -> None:
    # Issue #334 review: an accessible cover link followed by the title link
    # with the same URL stays one valid entry. Parser metadata classifies the
    # cover as non-book, so primary-link selection chooses the most title-like
    # non-cover anchor and grounded author evidence is parsed separately.
    body = (
        '<li><a href="https://example.test/books/q">Cover</a>'
        '<a href="https://example.test/books/q">Quantum Worlds</a>'
        '<span class="author">Jane Author</span></li>'
    )
    html = _page(body=f"<ul>{body}</ul>")
    entries = server_rendered_list_entries(html, SOURCE_URL)
    assert [(entry.title, entry.url) for entry in entries] == [
        ("Cover", "https://example.test/books/q"),
        ("Quantum Worlds", "https://example.test/books/q"),
    ]
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=html))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.reason_code == "ok"
    assert verdict.bounded_entry_count == 1
    assert verdict.samples == (
        EntrySample(title="Quantum Worlds", author="Jane Author", url="https://example.test/books/q"),
    )
    _sample_grounded(html, verdict.samples[0])


def test_cover_labels_are_parser_classified_as_chrome() -> None:
    # Issue #334 review: accessible cover labels are deterministic chrome,
    # never book-level links. Multi-word labels classify on a non-letter
    # separator prefix ("Cover of Quantum Worlds"), single-word labels only
    # exactly, so "Cover Story" stays a book title.
    assert is_cover_label("cover")
    assert is_cover_label("cover image")
    assert is_cover_label("cover of quantum worlds")
    assert is_cover_label("view cover image")
    assert is_cover_label("thumbnail")
    assert not is_cover_label("Cover Story")
    assert not is_cover_label("Covering")
    assert not is_cover_label("Quantum Worlds")
    html = _page(
        body=(
            "<ul>"
            '<li><a href="https://example.test/books/q">Cover of Quantum Worlds</a>'
            '<a href="https://example.test/books/q">Quantum Worlds</a></li>'
            "</ul>"
        )
    )
    entries = server_rendered_list_entries(html, SOURCE_URL)
    assert [(entry.title, entry.link_kind) for entry in entries] == [
        ("Cover of Quantum Worlds", LINK_KIND_ACTION),
        ("Quantum Worlds", "book"),
    ]


def test_cover_of_label_never_becomes_the_book_title() -> None:
    # Issue #334 review: a long accessible cover label must never become the
    # primary book title. "Cover of Quantum Worlds" carries more words than
    # the real title "Quantum Worlds", so word count alone cannot pick the
    # primary link: parser cover-label classification keeps the cover
    # anchor out of the book-level candidates, while the grounded author
    # evidence is parsed separately and the real title and book URL win.
    body = (
        '<li><a href="https://example.test/books/q">Cover of Quantum Worlds</a>'
        '<a href="https://example.test/books/q">Quantum Worlds</a>'
        '<span class="author">Jane Author</span></li>'
    )
    html = _page(body=f"<ul>{body}</ul>")
    entries = server_rendered_list_entries(html, SOURCE_URL)
    assert [(entry.title, entry.url) for entry in entries] == [
        ("Cover of Quantum Worlds", "https://example.test/books/q"),
        ("Quantum Worlds", "https://example.test/books/q"),
    ]
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=html))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.reason_code == "ok"
    assert verdict.bounded_entry_count == 1
    assert verdict.samples == (
        EntrySample(title="Quantum Worlds", author="Jane Author", url="https://example.test/books/q"),
    )
    _sample_grounded(html, verdict.samples[0])


def test_same_url_image_label_selects_exact_alternate_title() -> None:
    body = (
        '<li><a href="https://example.test/books/q">Image of Quantum Worlds</a>'
        '<a href="https://example.test/books/q">Quantum Worlds</a>'
        '<span class="author">Jane Author</span></li>'
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.samples == (
        EntrySample(title="Quantum Worlds", author="Jane Author", url="https://example.test/books/q"),
    )


def test_same_url_cover_colon_label_selects_exact_alternate_title() -> None:
    body = (
        '<li><a href="https://example.test/books/q">Cover: Quantum Worlds</a>'
        '<a href="https://example.test/books/q">Quantum Worlds</a>'
        '<span class="author">Jane Author</span></li>'
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.samples == (
        EntrySample(title="Quantum Worlds", author="Jane Author", url="https://example.test/books/q"),
    )


def test_standalone_image_and_cover_titles_remain_book_titles() -> None:
    for title in ("Image of Dorian Gray", "Cover Story"):
        body = f'<li><a href="https://example.test/books/q">{title}</a><span class="author">Jane Author</span></li>'
        verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
        assert verdict.outcome == OUTCOME_ACCEPT
        assert verdict.samples == (EntrySample(title=title, author="Jane Author", url="https://example.test/books/q"),)


def test_grounded_author_element_keeps_title_echo_in_author_name() -> None:
    body = '<li><a href="https://example.test/books/king">King</a><span class="author">Stephen King</span></li>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.samples == (EntrySample(title="King", author="Stephen King", url="https://example.test/books/king"),)


def test_bare_capitalized_prose_does_not_ground_author() -> None:
    body = '<li><a href="https://example.test/books/q">Quantum Worlds</a>Acclaimed Historian</li>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "missing_authors"
    assert verdict.samples == ()


def test_grounded_mononyms_and_coauthors_preserve_visible_spelling() -> None:
    for author in ("Cher", "Banksy", "Jane Doe and John Smith"):
        body = f'<li><a href="https://example.test/books/q">Quantum Worlds</a><span class="author">{author}</span></li>'
        verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
        assert verdict.outcome == OUTCOME_ACCEPT
        assert verdict.samples == (
            EntrySample(title="Quantum Worlds", author=author, url="https://example.test/books/q"),
        )


def test_typed_author_link_grounds_mononym_without_becoming_title() -> None:
    body = (
        '<li><a href="https://example.test/authors/cher">Cher</a>'
        '<a href="https://example.test/books/q">Quantum Worlds</a></li>'
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.samples == (EntrySample(title="Quantum Worlds", author="Cher", url="https://example.test/books/q"),)


def test_typed_author_link_keeps_title_echo_in_author_name() -> None:
    body = (
        '<li><a href="https://example.test/books/king">King</a>'
        '<a href="https://example.test/authors/stephen-king">Stephen King</a></li>'
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.samples == (EntrySample(title="King", author="Stephen King", url="https://example.test/books/king"),)


def test_bare_author_after_long_cover_label_rejects() -> None:
    # Issue #334 review: parser metadata classifies the cover label as a
    # non-book link, so primary-link selection chooses "Quantum Worlds".
    # The trailing "Jane Author" is unmarked context, not grounded author
    # evidence, so the evaluator rejects it fail-closed rather than
    # reconstructing an author from visible text.
    html = (
        "<h1>Books</h1>"
        '<li><a href="/books/q">Cover of Quantum Worlds</a>'
        '<a href="/books/q">Quantum Worlds</a>Jane Author</li>'
    )
    evidence = CapturedSourceEvidence(
        source_url="https://example.test/list", html=html, head_status=200, get_status=200
    )
    entries = server_rendered_list_entries(html, "https://example.test/list")
    assert [(entry.title, entry.link_kind) for entry in entries] == [
        ("Cover of Quantum Worlds", LINK_KIND_ACTION),
        ("Quantum Worlds", "book"),
    ]
    verdict = evaluate_captured_source_evidence(evidence)
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "missing_authors"
    assert verdict.bounded_entry_count == 1
    assert verdict.samples == ()
    # The raw fixture carries a relative href, so the resolved URL is not
    # textually grounded in the raw HTML even though the title and author are.
    assert "Quantum Worlds" in html
    assert "Jane Author" in html
    # Adding an explicit author element supplies grounded evidence, so the
    # marked author still wins when a long cover label precedes the title.
    marked = (
        "<h1>Books</h1>"
        '<li><a href="/books/q">Cover of Quantum Worlds</a>'
        '<a href="/books/q">Quantum Worlds</a><span class="author">Jane Author</span></li>'
    )
    evidence = CapturedSourceEvidence(
        source_url="https://example.test/list", html=marked, head_status=200, get_status=200
    )
    verdict = evaluate_captured_source_evidence(evidence)
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.reason_code == "ok"
    assert verdict.samples == (
        EntrySample(title="Quantum Worlds", author="Jane Author", url="https://example.test/books/q"),
    )


def test_grounded_multiword_names_are_accepted() -> None:
    # Issue #334 review: ordinary valid multiword names are accepted when
    # visibly grounded - through author metadata (an author-marked element)
    # or an explicit marker - and are not rejected solely because three or
    # more words share title-case capitalization. The same bare unmarked
    # text is ambiguous prose and fails closed.
    from autoscout.source_feasibility import _span_author

    assert _span_author("Mary Ann Evans") == ""
    assert _span_author("Gabriel Garcia Marquez") == ""
    for author in ("Mary Ann Evans", "Gabriel Garcia Marquez"):
        marked = (
            f'<li><a href="https://example.test/books/q">Quantum Worlds</a><span class="author">{author}</span></li>'
        )
        verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{marked}</ul>")))
        assert verdict.outcome == OUTCOME_ACCEPT
        assert verdict.reason_code == "ok"
        assert verdict.bounded_entry_count == 1
        assert verdict.samples == (
            EntrySample(title="Quantum Worlds", author=author, url="https://example.test/books/q"),
        )
        marker = (
            '<li><a href="https://example.test/books/q">Quantum Worlds</a>'
            f'<span class="primary-author">By: {author}</span></li>'
        )
        verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{marker}</ul>")))
        assert verdict.outcome == OUTCOME_ACCEPT
        assert verdict.reason_code == "ok"
        assert verdict.samples == (
            EntrySample(title="Quantum Worlds", author=author, url="https://example.test/books/q"),
        )
        bare = f'<li><a href="https://example.test/books/q">Quantum Worlds</a>{author}</li>'
        verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{bare}</ul>")))
        assert not verdict.accepted
        assert verdict.outcome == OUTCOME_REJECT
        assert verdict.reason_code == "missing_authors"
        assert verdict.samples == ()


def test_grounded_prose_is_still_rejected() -> None:
    # Issue #334 review: grounded author structure relaxes the name-shape
    # rules but never abandons span validation. Narrative, function, and
    # connector terms fail the extracted span closed even inside an
    # author-marked element or right after an explicit By marker.
    from autoscout.source_feasibility import _grounded_author_span

    assert _grounded_author_span("Powerful Exploration Across Europe") == ""
    assert _grounded_author_span("Winner of the annual prize") == ""
    for author in ("Powerful Exploration Across Europe", "Winner of the annual prize"):
        marked = (
            f'<li><a href="https://example.test/books/q">Quantum Worlds</a><span class="author">{author}</span></li>'
        )
        verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{marked}</ul>")))
        assert not verdict.accepted
        assert verdict.outcome == OUTCOME_REJECT
        assert verdict.reason_code == "missing_authors"
        assert verdict.samples == ()
        assert "Quantum Worlds" in verdict.reason
        marker = (
            '<li><a href="https://example.test/books/q">Quantum Worlds</a>'
            f'<span class="primary-author">By: {author}</span></li>'
        )
        verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{marker}</ul>")))
        assert not verdict.accepted
        assert verdict.outcome == OUTCOME_REJECT
        assert verdict.reason_code == "missing_authors"
        assert verdict.samples == ()


def test_nav_inside_article_stays_navigation_chrome() -> None:
    # Issue #334 review: a <nav> nested inside an article is still site
    # chrome, never a book link. Parser landmark metadata filters it from
    # primary-link selection and from grounded author evidence.
    html = _page(
        body=(
            "<article><nav>"
            '<a href="https://example.test/books/recommend">Recommended</a>'
            '</nav><span class="author">Jane Author</span>'
            '<a href="https://example.test/books/q">Quantum Worlds</a></article>'
        )
    )
    entries = server_rendered_list_entries(html, SOURCE_URL)
    assert [(entry.title, entry.in_nav, entry.link_kind, entry.block_tag) for entry in entries] == [
        ("Recommended", True, "navigation", "article"),
        ("Quantum Worlds", False, "book", "article"),
    ]
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=html))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.reason_code == "ok"
    assert verdict.bounded_entry_count == 1
    assert verdict.samples == (
        EntrySample(title="Quantum Worlds", author="Jane Author", url="https://example.test/books/q"),
    )


def test_decomposed_accent_name_is_preserved() -> None:
    # Issue #334 review: decomposed accented text (L + U+0301 COMBINING
    # ACUTE ACCENT) is Unicode-safe: the returned author keeps the exact
    # visible spelling from the captured HTML and is never normalized or
    # split by the combining mark.
    from autoscout.source_feasibility import _grounded_author_span
    from autoscout.source_feasibility import _span_author

    decomposed = "Lu\u0301kasz Orbitowski"
    assert _span_author(decomposed) == ""
    assert _grounded_author_span(decomposed) == decomposed
    body = f'<li><a href="https://example.test/books/q">Quantum Worlds</a><span class="author">{decomposed}</span></li>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.reason_code == "ok"
    assert verdict.samples == (
        EntrySample(title="Quantum Worlds", author=decomposed, url="https://example.test/books/q"),
    )
    _sample_grounded(_page(body=f"<ul>{body}</ul>"), verdict.samples[0])


def test_marker_payload_keeps_title_word_in_author() -> None:
    body = '<li><a href="/books/king">King</a>By: Stephen King</li>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.samples == (EntrySample(title="King", author="Stephen King", url="https://example.test/books/king"),)


def test_marker_before_title_keeps_marker_payload_and_title_by_is_not_marker() -> None:
    before_title = '<li>By: Stephen King<a href="/books/king">King</a></li>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{before_title}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.samples == (EntrySample(title="King", author="Stephen King", url="https://example.test/books/king"),)

    for title in ("By the River", "By: The River", "By: Jane Doe"):
        title_only = f'<li><a href="/books/by-the-river">{title}</a></li>'
        verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{title_only}</ul>")))
        assert verdict.outcome == OUTCOME_REJECT
        assert verdict.reason_code == "missing_authors"


def test_comma_separated_grounded_author_preserves_punctuation() -> None:
    body = '<li><a href="/books/q">Quantum Worlds</a><span class="author">Jane Doe, John Smith</span></li>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.samples == (
        EntrySample(title="Quantum Worlds", author="Jane Doe, John Smith", url="https://example.test/books/q"),
    )


def test_generic_profile_label_is_not_url_only_author_evidence() -> None:
    body = '<li><a href="/books/q">Quantum Worlds</a><a href="/profiles/john">Profile</a></li>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "missing_authors"


def test_generic_profile_before_real_linked_author_does_not_replace_author() -> None:
    body = (
        '<li><a href="/profiles/john">Profile</a>'
        '<a href="/authors/jane-author">Jane Author</a>'
        '<a href="/books/q">Quantum Worlds</a></li>'
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.samples == (
        EntrySample(title="Quantum Worlds", author="Jane Author", url="https://example.test/books/q"),
    )


def test_distinct_url_only_author_candidates_are_ambiguous() -> None:
    body = (
        '<li><a href="/contributors/first-contributor">First Contributor</a>'
        '<a href="/authors/second-author">Second Author</a>'
        '<a href="/books/q">Quantum Worlds</a></li>'
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "missing_authors"
    assert verdict.samples == ()


def test_explicit_author_evidence_overrides_url_only_contributor() -> None:
    body = (
        '<li><a href="/contributors/john-smith">John Smith</a>'
        '<span class="author">Jane Doe</span>'
        '<a href="/books/q">Quantum Worlds</a></li>'
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.samples == (
        EntrySample(title="Quantum Worlds", author="Jane Doe", url="https://example.test/books/q"),
    )


def test_profile_cta_text_is_grounded_to_the_url_identity() -> None:
    body = '<li><a href="/books/q">Quantum Worlds</a><a href="/profiles/jane-doe">View Jane Doe</a></li>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.samples == (
        EntrySample(title="Quantum Worlds", author="Jane Doe", url="https://example.test/books/q"),
    )


@pytest.mark.parametrize("attributes", ('rel="author"', 'itemprop="author"', 'class="author"'))
def test_generic_explicit_author_labels_are_rejected(attributes: str) -> None:
    body = f'<li><a href="/books/q">Quantum Worlds</a><a {attributes} href="/profiles/jane-doe">Profile</a></li>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "missing_authors"
    assert verdict.samples == ()


def test_url_inferred_lowercase_name_remains_valid() -> None:
    body = '<li><a href="/books/q">Quantum Worlds</a><a href="/profiles/bell-hooks">bell hooks</a></li>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.samples == (
        EntrySample(title="Quantum Worlds", author="bell hooks", url="https://example.test/books/q"),
    )


def test_colon_marked_author_link_is_scoped_to_a_bounded_book_entry() -> None:
    body = '<li><a href="/books/q">Quantum Worlds</a><a href="/jane-doe">By: Jane Doe</a></li>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.samples == (
        EntrySample(title="Quantum Worlds", author="Jane Doe", url="https://example.test/books/q"),
    )


def test_colon_marked_lone_book_title_stays_a_book_title() -> None:
    body = '<li><a href="/books/by-the-river">By: The River</a></li>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "missing_authors"
    assert verdict.bounded_entry_count == 1
    assert verdict.samples == ()


@pytest.mark.parametrize(
    "nested_evidence",
    (
        '<a href="/authors/jane-doe">Jane Doe</a>',
        '<a rel="author" href="/jane-doe">Jane Doe</a>',
        '<span class="author">Jane Doe</span>',
    ),
)
def test_nested_non_product_author_evidence_grounds_enclosing_product(nested_evidence: str) -> None:
    body = f'<article><a href="/books/q">Quantum Worlds</a><section>{nested_evidence}</section></article>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=body)))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.samples == (
        EntrySample(title="Quantum Worlds", author="Jane Doe", url="https://example.test/books/q"),
    )


def test_nested_product_evidence_stays_separate_from_enclosing_product() -> None:
    body = (
        '<article><a href="/books/outer">Outer Book</a><span class="author">Outer Author</span>'
        '<article><a href="/books/inner">Inner Book</a><span class="author">Nested Author</span></article>'
        "</article>"
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=body)))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.bounded_entry_count == 2
    assert verdict.samples == (
        EntrySample(title="Outer Book", author="Outer Author", url="https://example.test/books/outer"),
        EntrySample(title="Inner Book", author="Nested Author", url="https://example.test/books/inner"),
    )


@pytest.mark.parametrize("author", ("Jane Doe, John Smith, and Bob Ray", "Jane Doe, and John Smith"))
def test_oxford_comma_coauthors_preserve_visible_punctuation(author: str) -> None:
    body = f'<li><a href="/books/q">Quantum Worlds</a><span class="author">{author}</span></li>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.samples == (EntrySample(title="Quantum Worlds", author=author, url="https://example.test/books/q"),)


@pytest.mark.parametrize("author", ("Jane Doe, and", "Jane Doe, John Smith, or Bob Ray", "Jane Doe, and the River"))
def test_malformed_oxford_comma_coauthors_are_rejected(author: str) -> None:
    body = f'<li><a href="/books/q">Quantum Worlds</a><span class="author">{author}</span></li>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "missing_authors"
    assert verdict.samples == ()


@pytest.mark.parametrize("author", ("Elliot Page", "Larry Page", "Page McConnell"))
def test_page_is_valid_when_grounded_as_an_explicit_author(author: str) -> None:
    body = f'<li><a href="/books/q">Quantum Worlds</a><span class="author">{author}</span></li>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.samples == (EntrySample(title="Quantum Worlds", author=author, url="https://example.test/books/q"),)


@pytest.mark.parametrize("label", ("See Jane", "Meet Jane", "Author Jane"))
def test_one_token_url_identity_does_not_absorb_cta_words(label: str) -> None:
    body = f'<li><a href="/books/q">Quantum Worlds</a><a href="/profiles/jane">{label}</a></li>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.samples == (EntrySample(title="Quantum Worlds", author="Jane", url="https://example.test/books/q"),)


def test_one_token_url_identity_accepts_exact_visible_name() -> None:
    body = '<li><a href="/books/q">Quantum Worlds</a><a href="/profile/jane">Jane</a></li>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=f"<ul>{body}</ul>")))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.samples == (EntrySample(title="Quantum Worlds", author="Jane", url="https://example.test/books/q"),)


def test_navigation_marker_text_does_not_override_entry_author() -> None:
    body = (
        "<article><nav>By: Site Editor</nav>"
        '<a href="/books/q">Quantum Worlds</a><span class="author">Jane Doe</span></article>'
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=body)))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.samples == (
        EntrySample(title="Quantum Worlds", author="Jane Doe", url="https://example.test/books/q"),
    )


def test_per_entry_header_marker_grounds_book_inside_header() -> None:
    body = '<article><header><a href="/books/q">Quantum Worlds</a><span>By: Jane Doe</span></header></article>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=body)))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.samples == (
        EntrySample(title="Quantum Worlds", author="Jane Doe", url="https://example.test/books/q"),
    )


def test_per_entry_header_marker_grounds_book_elsewhere_in_article() -> None:
    body = '<article><header>By: Jane Doe</header><a href="/books/q">Quantum Worlds</a></article>'
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=body)))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.samples == (
        EntrySample(title="Quantum Worlds", author="Jane Doe", url="https://example.test/books/q"),
    )


def test_outer_author_element_survives_nested_entry_block() -> None:
    body = (
        '<article><a href="/books/q">Quantum Worlds</a><div class="author">Jane<section></section> Doe</div></article>'
    )
    verdict = evaluate_captured_source_evidence(_verdict_kwargs(html=_page(body=body)))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.samples == (
        EntrySample(title="Quantum Worlds", author="Jane Doe", url="https://example.test/books/q"),
    )
