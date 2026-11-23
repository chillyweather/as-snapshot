"""Amazon Charts Most Read Nonfiction onboarding contract (Issue #370)."""

from __future__ import annotations

import html as html_lib
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from autoscout.config import validate_config
from autoscout.source_collection import SourceCollection
from autoscout.source_collectors import CollectorRunError
from autoscout.source_collectors import extraction_contract_violation
from autoscout.source_collectors import fetch_agent_source_page
from autoscout.source_feasibility import evaluate_amazon_charts_feasibility
from autoscout.source_page_identity import amazon_charts_entries
from autoscout.source_page_identity import amazon_charts_mention_is_grounded
from autoscout.source_page_identity import amazon_charts_ranks
from autoscout.source_page_identity import amazon_charts_week_url
from autoscout.source_page_identity import page_title_text
from autoscout.source_page_identity import validate_page_shape

LANDING_URL = "https://www.amazon.com/charts"
DATED_URL = "https://www.amazon.com/charts/2026-07-26/mostread/nonfiction"

ROWS = [
    (1, "Regime Change", "Maggie Haberman & Jonathan Swan", 5, "1668067242"),
    (2, "The Let Them Theory", "Mel Robbins & Sawyer Robbins", 83, "1401971369"),
    (3, "Strangers", "Belle Burden", 28, "B0F3WTJ9V2"),
    (4, "Atomic Habits", "James Clear", 397, "0735211299"),
    (5, "Famesick", "Lena Dunham", 15, "B0FKGJ26MK"),
    (6, "London Falling", "Patrick Radden Keefe", 15, "B0FGZYTRP1"),
    (7, "The 48 Laws of Power", "Robert Greene", 263, "B0024CEZR6"),
    (8, "The Psychology of Money", "Morgan Housel", 162, "0857197681"),
    (9, "Stop Letting Everything Affect You", "Daniel Chidiac", 24, "1764110803"),
    (10, "The Body Keeps the Score", "Bessel van der Kolk", 258, "B00G3L1C2K"),
    (11, "The Daily Stoic", "Ryan Holiday & Stephen Hanselman", 287, "B01HNJIJB2"),
    (12, "The Land and Its People", "David Sedaris", 9, "0316264830"),
    (13, "Sapiens", "Yuval Noah Harari", 481, "B00ICN066A"),
    (14, "1929", "Andrew Ross Sorkin", 41, "0593296966"),
    (15, "How to Win Friends and Influence People", "Dale Carnegie", 290, "1439167346"),
    (16, "If You Tell", "Gregg Olsen", 275, "B07Q5TL9SQ"),
    (17, "The Subtle Art of Not Giving a F*ck", "Mark Manson", 449, "B019MMUA8S"),
    (18, "Can't Hurt Me", "David Goggins", 380, "B07H453KGH"),
    (19, "Cancel Me If You Can", "Dave Portnoy", 4, "1668222027"),
    (20, "The Power of Now", "Eckhart Tolle", 149, "1577314808"),
]


def chart_block(row: tuple[int, str, str, int, str]) -> str:
    rank, title, author, weeks, asin = row
    escaped_title = html_lib.escape(title)
    escaped_author = html_lib.escape(author)
    return (
        f'<div id="rank{rank}" class="kc-rank-card tablet-hide kc-four-columns" data-rank="{rank}">'
        f'{rank}<a class="kc-cover-link" href="/dp/{asin}/ref=chrt_bk_rd_nf_{rank}_ci_lp">'
        f'<img alt="Cover image of {escaped_title} by {escaped_author}"></a>'
        f'<div class="kc-wol">{weeks} weeks on the list</div>'
        f'<div class="kc-rank-card-title">{escaped_title}</div>'
        f'<div class="kc-rank-card-author">by {escaped_author}</div>'
        "</div>"
    )


CHART_BLOCKS = [chart_block(row) for row in ROWS]
CHART_HTML = "<title>Most Read Nonfiction | Amazon Charts</title>" + "".join(CHART_BLOCKS)
LANDING_HTML = (
    '<a href="/charts/2026-07-26/mostread/fiction">fiction</a>'
    '<a href="/charts/2026-07-26/mostread/nonfiction?ref=chrt_bk_dx_intra_nf_rd#top">nonfiction</a>'
    '<a href="/charts/2026-07-26/mostsold/fiction">sold fiction</a>'
)
FICTION_HTML = "<title>Most Read Fiction | Amazon Charts</title>" + "".join(CHART_BLOCKS)


def source(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": "Amazon Charts Most Read Nonfiction",
        "family": "bestseller/ranking",
        "url": LANDING_URL,
        "active": True,
        "access_status": "open",
        "language": "en",
        "signal_family": "bestseller/ranking",
        "expected_page_heading": "Most Read Nonfiction",
        "chart_path_segment": "mostread/nonfiction",
        "author_marker": "by",
        "metadata_exact_book_list": True,
        "amazon_charts_weekly": True,
        "max_response_bytes": 750_000,
        "expected_signal_strength": "top ranking",
        "eligible_candidate_category": "adult nonfiction",
        "extraction_method": "agent-extraction-replay",
    }
    result.update(overrides)
    return result


def page(html: str = CHART_HTML) -> dict[str, str]:
    return {"source_page": DATED_URL, "resolved_url": DATED_URL, "html": html}


class _FakeResponse:
    def __init__(self, body: str, resolved_url: str) -> None:
        self.body = body.encode()
        self.resolved_url = resolved_url

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.body[:limit]

    def geturl(self) -> str:
        return self.resolved_url

    def getheader(self, _name: str, default: str = "") -> str:
        return default


# A live fetch judges week freshness against the clock, so the live-path tests
# pin the clock rather than a configured run_date.
WEEK_REFERENCE = date(2026, 7, 26)


def direct_evidence_url(asin: str) -> str:
    return f"{DATED_URL.split('/charts/')[0]}/dp/{asin}"


def extraction_mentions() -> list[dict[str, Any]]:
    return [
        {
            "title": title,
            "primary_authors": [author],
            "source_url": DATED_URL,
            "evidence_url": direct_evidence_url(asin),
            "signal_type": "bestseller/ranking",
            "signal_strength": "top ranking",
            "candidate_category": "adult nonfiction",
            "current_rank": rank,
            "weeks_on_list": weeks,
            "evidence_note": f"Amazon Charts rank {rank}; {weeks} weeks on the list.",
        }
        for rank, title, author, weeks, asin in ROWS
    ]


def test_fixture_is_compact_and_page_identity_is_deterministic() -> None:
    assert len(CHART_HTML) < 8000
    assert page_title_text("<title>  Most&nbsp;Read   Nonfiction | Amazon Charts </title>") == (
        "Most Read Nonfiction | Amazon Charts"
    )
    assert page_title_text("<html><body>no title</body></html>") == ""
    reference = date(2026, 7, 26)
    assert (
        amazon_charts_week_url(
            LANDING_HTML,
            LANDING_URL,
            chart_path_segment="mostread/nonfiction",
            expected_page_heading="Most Read Nonfiction",
            reference_date=reference,
        )
        == DATED_URL
    )
    # The explicit segment is the production contract; the heading-derived
    # fallback stays available to direct callers that predate the key.
    assert (
        amazon_charts_week_url(
            LANDING_HTML,
            LANDING_URL,
            expected_page_heading="Most Read Nonfiction",
            reference_date=reference,
        )
        == DATED_URL
    )
    with pytest.raises(ValueError):
        amazon_charts_week_url(
            '<a href="/charts/2026-07-26/mostread/fiction">wrong</a>',
            LANDING_URL,
            expected_page_heading="Most Read Nonfiction",
            reference_date=reference,
        )
    with pytest.raises(ValueError):
        amazon_charts_week_url(
            '<a href="/charts/not-a-date/mostread/nonfiction">wrong</a>',
            LANDING_URL,
            expected_page_heading="Most Read Nonfiction",
            reference_date=reference,
        )
    with pytest.raises(ValueError):
        amazon_charts_week_url(
            '<a href="/charts/2026-07-26/mostread/nonfiction">relative base</a>',
            "/charts",
            expected_page_heading="Most Read Nonfiction",
            reference_date=reference,
        )
    with pytest.raises(ValueError):
        amazon_charts_week_url(
            '<a href="/charts/2026-07-26/mostread/nonfiction?page=2">bad</a>',
            LANDING_URL,
            expected_page_heading="Most Read Nonfiction",
            reference_date=reference,
        )
    with pytest.raises(ValueError):
        amazon_charts_week_url(
            '<a href="/charts/2026-07-26/mostread/nonfiction?pg=2">bad</a>',
            LANDING_URL,
            expected_page_heading="Most Read Nonfiction",
            reference_date=reference,
        )
    with pytest.raises(ValueError):
        amazon_charts_week_url(
            '<a href="/charts/2026-07-26/mostread/nonfiction?offset=10">bad</a>',
            LANDING_URL,
            expected_page_heading="Most Read Nonfiction",
            reference_date=reference,
        )
    with pytest.raises(ValueError):
        amazon_charts_week_url(
            '<a href="/charts/2026-06-28/mostread/nonfiction">stale</a>',
            LANDING_URL,
            expected_page_heading="Most Read Nonfiction",
            reference_date=reference,
        )
    with pytest.raises(ValueError):
        amazon_charts_week_url(
            '<a href="/charts/2026-08-16/mostread/nonfiction">future</a>',
            LANDING_URL,
            expected_page_heading="Most Read Nonfiction",
            reference_date=reference,
        )
    assert (
        amazon_charts_week_url(
            '<a href="/charts/2026-07-26/mostread/nonfiction?foo=1">ok</a>',
            LANDING_URL,
            chart_path_segment="mostread/nonfiction",
            expected_page_heading="Most Read Nonfiction",
            reference_date=reference,
        )
        == DATED_URL
    )
    assert (
        amazon_charts_week_url(
            '<a href="/charts/2026-07-26/mostread/nonfiction?ref=a&ref=b">ok</a>',
            LANDING_URL,
            chart_path_segment="mostread/nonfiction",
            expected_page_heading="Most Read Nonfiction",
            reference_date=reference,
        )
        == DATED_URL
    )
    # Absolute Amazon hrefs are accepted; links inside script blocks are ignored.
    assert (
        amazon_charts_week_url(
            '<a href="https://www.amazon.com/charts/2026-07-26/mostread/nonfiction?ref=x">ok</a>',
            LANDING_URL,
            chart_path_segment="mostread/nonfiction",
            expected_page_heading="Most Read Nonfiction",
            reference_date=reference,
        )
        == DATED_URL
    )
    assert (
        amazon_charts_week_url(
            '<script>var x="/charts/2026-07-26/mostread/nonfiction";</script>'
            '<a href="/charts/2026-07-26/mostread/nonfiction?ref=x">ok</a>',
            LANDING_URL,
            chart_path_segment="mostread/nonfiction",
            expected_page_heading="Most Read Nonfiction",
            reference_date=reference,
        )
        == DATED_URL
    )
    # A landing page on a different marketplace host resolves only its own
    # host: an absolute www.amazon.de link must not resolve on www.amazon.com.
    with pytest.raises(ValueError):
        amazon_charts_week_url(
            '<a href="https://www.amazon.de/charts/2026-07-26/mostread/nonfiction">de</a>',
            LANDING_URL,
            chart_path_segment="mostread/nonfiction",
            expected_page_heading="Most Read Nonfiction",
            reference_date=reference,
        )


def test_week_resolution_rejects_prefixed_paths_and_skips_malformed_dates() -> None:
    reference = date(2026, 7, 26)
    valid = '<a href="/charts/2026-07-26/mostread/nonfiction">current</a>'
    malformed = '<a href="/charts/2026-99-99/mostread/nonfiction">malformed</a>'
    assert (
        amazon_charts_week_url(
            malformed + valid,
            LANDING_URL,
            chart_path_segment="mostread/nonfiction",
            expected_page_heading="Most Read Nonfiction",
            reference_date=reference,
        )
        == DATED_URL
    )
    with pytest.raises(ValueError):
        amazon_charts_week_url(
            '<a href="/x/charts/2026-07-26/mostread/nonfiction">prefixed</a>',
            LANDING_URL,
            chart_path_segment="mostread/nonfiction",
            expected_page_heading="Most Read Nonfiction",
            reference_date=reference,
        )


def test_week_resolution_prefers_the_newest_in_window_link() -> None:
    """An archive link rendered before the current week must not win the resolution."""
    reference = date(2026, 7, 28)
    previous_week = '<a href="/charts/2026-07-19/mostread/nonfiction">previous week</a>'
    current_week = '<a href="/charts/2026-08-02/mostread/nonfiction?ref=x">current week</a>'
    newest = "https://www.amazon.com/charts/2026-08-02/mostread/nonfiction"
    for landing_html in (previous_week + current_week, current_week + previous_week):
        assert (
            amazon_charts_week_url(
                landing_html,
                LANDING_URL,
                expected_page_heading="Most Read Nonfiction",
                reference_date=reference,
            )
            == newest
        )
    # Repeated links to one week keep document order and stay deterministic.
    assert (
        amazon_charts_week_url(
            current_week + current_week,
            LANDING_URL,
            expected_page_heading="Most Read Nonfiction",
            reference_date=reference,
        )
        == newest
    )


def test_entries_ranks_and_grounding() -> None:
    entries = amazon_charts_entries(CHART_HTML, DATED_URL)
    assert len(entries) == 20
    assert [entry[1] for entry in entries] == [row[1] for row in ROWS]
    assert [entry[0] for entry in entries] == [
        f"{DATED_URL.split('/charts/')[0]}/dp/{row[4]}/ref=chrt_bk_rd_nf_{row[0]}_ci_lp" for row in ROWS
    ]
    assert all(row[2].casefold() in entry[2].casefold() for row, entry in zip(ROWS, entries, strict=True))
    assert amazon_charts_ranks(CHART_HTML, DATED_URL) == list(range(1, 21))
    first = ROWS[0]
    evidence_url = direct_evidence_url(first[4])
    assert amazon_charts_mention_is_grounded(
        CHART_HTML,
        DATED_URL,
        rank=first[0],
        title=first[1],
        authors=[first[2]],
        evidence_url=evidence_url,
    )
    assert not amazon_charts_mention_is_grounded(
        CHART_HTML, DATED_URL, rank=2, title=first[1], authors=[first[2]], evidence_url=evidence_url
    )
    assert not amazon_charts_mention_is_grounded(
        CHART_HTML, DATED_URL, rank=1, title="Wrong title", authors=[first[2]], evidence_url=evidence_url
    )
    assert not amazon_charts_mention_is_grounded(
        CHART_HTML, DATED_URL, rank=1, title=first[1], authors=["Missing Author"], evidence_url=evidence_url
    )
    assert not amazon_charts_mention_is_grounded(
        CHART_HTML,
        DATED_URL,
        rank=1,
        title=first[1],
        authors=[first[2]],
        evidence_url=direct_evidence_url("0000000000"),
    )


def test_amazon_charts_accepts_lowercase_asins_and_bounds_author_matches() -> None:
    lower_asin = ROWS[2][4].lower()
    lower_html = CHART_HTML.replace(f"/dp/{ROWS[2][4]}/", f"/dp/{lower_asin}/")
    lower_entries = amazon_charts_entries(lower_html, DATED_URL)
    assert lower_entries[2][0].startswith(f"{DATED_URL.split('/charts/')[0]}/dp/{lower_asin}/")
    assert amazon_charts_mention_is_grounded(
        lower_html,
        DATED_URL,
        rank=ROWS[2][0],
        title=ROWS[2][1],
        authors=[ROWS[2][2]],
        evidence_url=direct_evidence_url(lower_asin),
    )

    partial_word_html = CHART_HTML.replace(html_lib.escape(ROWS[0][2]), "Joann Smith")
    assert not amazon_charts_mention_is_grounded(
        partial_word_html,
        DATED_URL,
        rank=ROWS[0][0],
        title=ROWS[0][1],
        authors=["ann"],
        evidence_url=direct_evidence_url(ROWS[0][4]),
    )
    assert amazon_charts_mention_is_grounded(
        partial_word_html,
        DATED_URL,
        rank=ROWS[0][0],
        title=ROWS[0][1],
        authors=["Joann Smith"],
        evidence_url=direct_evidence_url(ROWS[0][4]),
    )


def test_page_shape_requires_nonfiction_title_and_all_ranks() -> None:
    configured = source()
    assert validate_page_shape(configured, page()) == ""
    fiction_error = validate_page_shape(configured, page(FICTION_HTML))
    assert fiction_error and "title" in fiction_error
    assert validate_page_shape(
        configured, page("<title>Most Read Nonfiction | Amazon Charts</title>" + "".join(CHART_BLOCKS[:-1]))
    )


def test_feasibility_requires_dated_chart_evidence() -> None:
    configured = source()
    landing = '<a href="/charts/2026-07-26/mostread/nonfiction">current</a>'
    reference = date(2026, 7, 26)
    accepted = evaluate_amazon_charts_feasibility(
        configured,
        head_status=405,
        get_status=200,
        resolved_url=DATED_URL,
        html=CHART_HTML,
        landing_html=landing,
        reference_date=reference,
    )
    assert accepted.accepted and accepted.use_get_fallback
    # Landing evidence is mandatory: an empty or blank capture rejects even
    # when the dated page evidence is valid.
    empty_landing = evaluate_amazon_charts_feasibility(
        configured,
        head_status=405,
        get_status=200,
        resolved_url=DATED_URL,
        html=CHART_HTML,
        landing_html="",
        reference_date=reference,
    )
    assert not empty_landing.accepted and "landing page evidence is required" in empty_landing.reason
    blank_landing = evaluate_amazon_charts_feasibility(
        configured,
        head_status=405,
        get_status=200,
        resolved_url=DATED_URL,
        html=CHART_HTML,
        landing_html="   \n  ",
        reference_date=reference,
    )
    assert not blank_landing.accepted and "landing page evidence is required" in blank_landing.reason
    # The recorded landing evidence must resolve a dated link for the
    # configured segment; a landing page without one fails closed.
    wrong_segment_landing = evaluate_amazon_charts_feasibility(
        configured,
        head_status=405,
        get_status=200,
        resolved_url=DATED_URL,
        html=CHART_HTML,
        landing_html='<a href="/charts/2026-07-26/mostread/fiction">wrong</a>',
        reference_date=reference,
    )
    assert not wrong_segment_landing.accepted and "no recent dated link" in wrong_segment_landing.reason
    # A bot wall on the dated page fails closed even with valid landing
    # evidence.
    bot_wall = evaluate_amazon_charts_feasibility(
        configured,
        head_status=405,
        get_status=200,
        resolved_url=DATED_URL,
        html=CHART_HTML + "robot check",
        landing_html=landing,
        reference_date=reference,
    )
    assert not bot_wall.accepted and "bot wall" in bot_wall.reason
    # A fiction heading fails the nonfiction page-title check.
    fiction = evaluate_amazon_charts_feasibility(
        configured,
        head_status=405,
        get_status=200,
        resolved_url=DATED_URL,
        html=FICTION_HTML,
        landing_html=landing,
        reference_date=reference,
    )
    assert not fiction.accepted and "page title" in fiction.reason
    # A resolved URL on another host, scheme, or segment is not the dated
    # chart for the configured landing page.
    for other_url in (
        DATED_URL.replace("www.amazon.com", "www.example.com"),
        DATED_URL.replace("https://", "http://"),
        DATED_URL.replace("nonfiction", "fiction"),
    ):
        other = evaluate_amazon_charts_feasibility(
            configured,
            head_status=405,
            get_status=200,
            resolved_url=other_url,
            html=CHART_HTML,
            landing_html=landing,
            reference_date=reference,
        )
        assert not other.accepted and "not a dated chart for the configured segment" in other.reason, other_url
    # A non-200 GET fails closed before any page evidence is consulted.
    failed_get = evaluate_amazon_charts_feasibility(
        configured,
        head_status=405,
        get_status=204,
        resolved_url=DATED_URL,
        html=CHART_HTML,
        landing_html=landing,
        reference_date=reference,
    )
    assert not failed_get.accepted and "GET must return 200" in failed_get.reason
    # A truncated rank set fails the exact-20-entry contract.
    truncated = evaluate_amazon_charts_feasibility(
        configured,
        head_status=405,
        get_status=200,
        resolved_url=DATED_URL,
        html="<title>Most Read Nonfiction | Amazon Charts</title>" + "".join(CHART_BLOCKS[:-1]),
        landing_html=landing,
        reference_date=reference,
    )
    assert not truncated.accepted and "ranks 1 through 20" in truncated.reason


def test_feasibility_author_marker_is_configurable_per_source() -> None:
    landing = '<a href="/charts/2026-07-26/mostread/nonfiction">current</a>'
    reference = date(2026, 7, 26)
    # The English chart uses the "by" marker: with the German marker
    # configured, no entry exposes a nonblank author and the seam rejects.
    german_marker = evaluate_amazon_charts_feasibility(
        source(author_marker="von"),
        head_status=200,
        get_status=200,
        resolved_url=DATED_URL,
        html=CHART_HTML,
        landing_html=landing,
        reference_date=reference,
    )
    assert not german_marker.accepted and "nonblank author" in german_marker.reason
    # An explicit marker override wins over the configured one.
    accepted = evaluate_amazon_charts_feasibility(
        source(author_marker="von"),
        head_status=200,
        get_status=200,
        resolved_url=DATED_URL,
        html=CHART_HTML,
        landing_html=landing,
        reference_date=reference,
        author_marker="by",
    )
    assert accepted.accepted
    # A mixed-case configured marker is casefolded before matching against
    # the casefolded visible text, so "By" must satisfy the English chart
    # exactly like "by".
    mixed_case = evaluate_amazon_charts_feasibility(
        source(author_marker="By"),
        head_status=200,
        get_status=200,
        resolved_url=DATED_URL,
        html=CHART_HTML,
        landing_html=landing,
        reference_date=reference,
    )
    assert mixed_case.accepted


def test_config_validation_enforces_amazon_charts_contract() -> None:
    valid = {"run_date": "2026-07-26", "sources": [source()]}
    assert validate_config(valid) == []
    for override in (
        {"family": "media/curated list"},
        {"url": "http://www.amazon.com/charts"},
        {"url": f"{LANDING_URL}?ref=bad"},
        {"metadata_exact_book_list": False},
        {"max_response_bytes": 749_999},
        {"expected_signal_strength": "minor mention"},
        {"language": "de"},  # host/language mismatch
        {"url": "https://www.amazon.de/charts"},  # host/language mismatch
        {"url": "https://www.amazon.fr/charts"},  # unsupported host
        {"chart_path_segment": ""},
        {"chart_path_segment": "MostRead/Nonfiction"},
        {"chart_path_segment": "mostread"},
        {"author_marker": ""},
        {"author_marker": "by author"},
    ):
        errors = validate_config({"run_date": "2026-07-26", "sources": [source(**override)]})
        assert errors, override


def test_config_validation_accepts_uk_marketplace() -> None:
    uk = source(
        name="Amazon UK Charts Most Read Nonfiction",
        url="https://www.amazon.co.uk/charts",
        language="en",
        country="United Kingdom",
        market="United Kingdom",
        expected_page_heading="Most Read Nonfiction",
        chart_path_segment="mostread/nonfiction",
        author_marker="by",
    )
    assert validate_config({"run_date": "2026-07-26", "sources": [uk]}) == []
    # The pairing stays strict: the same host with a non-English language
    # must be rejected.
    mismatch = {**uk, "language": "de"}
    assert validate_config({"run_date": "2026-07-26", "sources": [mismatch]})


def test_config_validation_accepts_german_marketplace() -> None:
    german = source(
        name="Amazon.de Charts Meistgelesene Sachbücher",
        url="https://www.amazon.de/charts",
        language="de",
        country="Germany",
        market="Germany",
        expected_page_heading="Meistgelesene Sachbücher",
        chart_path_segment="mostread/nonfiction",
        author_marker="von",
    )
    assert validate_config({"run_date": "2026-08-04", "sources": [german]}) == []


def test_amazon_extraction_contract_is_strict() -> None:
    configured = source()
    valid = extraction_mentions()[0]
    assert extraction_contract_violation(valid, valid, configured, page()) == ""
    digit_strings = {**valid, "current_rank": "1", "weeks_on_list": "5"}
    assert extraction_contract_violation(digit_strings, digit_strings, configured, page()) == ""
    assert extraction_contract_violation(
        {**valid, "current_rank": True}, {**valid, "current_rank": True}, configured, page()
    )
    assert extraction_contract_violation(
        {**valid, "weeks_on_list": "abc"}, {**valid, "weeks_on_list": "abc"}, configured, page()
    )
    for override in (
        {"source_url": LANDING_URL},
        {"source_url": DATED_URL + "?tracking=1"},
        {"source_url": DATED_URL + "#fragment"},
        {"evidence_url": f"{DATED_URL}/gp/aw/d/{ROWS[0][4]}"},
        {"current_rank": 21},
        {"evidence_note": "Print sales were high."},
        {"candidate_category": "fiction"},
        {"signal_strength": "winner"},
        {"signal_type": "audiobook ranking"},
    ):
        invalid = {**valid, **override}
        assert extraction_contract_violation(invalid, invalid, configured, page())
    ungrounded = {**valid, "title": "Not on this chart"}
    assert extraction_contract_violation(ungrounded, ungrounded, configured, page())
    missing_weeks = {key: value for key, value in valid.items() if key != "weeks_on_list"}
    assert extraction_contract_violation(missing_weeks, missing_weeks, configured, page())
    sales_title_note = {**valid, "evidence_note": "Current rank 1 for Sales Superstars."}
    assert extraction_contract_violation(sales_title_note, sales_title_note, configured, page()) == ""
    for note in ("Sales were noted.", "Reviews were noted."):
        allowed_bare_word_note = {**valid, "evidence_note": note}
        assert extraction_contract_violation(allowed_bare_word_note, allowed_bare_word_note, configured, page()) == ""
    note_violation = {**valid, "evidence_note": "Print sales were high."}
    assert (
        extraction_contract_violation(note_violation, note_violation, configured, page())
        == "Amazon Charts weekly evidence_note must be concise metadata-only without print sales, sales units, "
        "units sold, copies sold, promotional prose, review text, or description text"
    )


def test_amazon_extraction_contract_accepts_lowercase_asin_links() -> None:
    configured = source()
    row = ROWS[2]
    lower_asin = row[4].lower()
    lower_html = CHART_HTML.replace(f"/dp/{row[4]}/", f"/dp/{lower_asin}/")
    lower_page = page(lower_html)
    mention = next(item for item in extraction_mentions() if item["current_rank"] == row[0])
    mention = {**mention, "evidence_url": direct_evidence_url(lower_asin)}
    assert extraction_contract_violation(mention, mention, configured, lower_page) == ""
    suffix_evidence = {**mention, "evidence_url": f"{direct_evidence_url(lower_asin)}/ref=tracking"}
    assert extraction_contract_violation(suffix_evidence, suffix_evidence, configured, lower_page) == ""


def test_replay_collects_all_twenty_mentions(tmp_path: Path) -> None:
    page_path = tmp_path / "chart-page.json"
    extraction_path = tmp_path / "chart-extraction.json"
    page_path.write_text(json.dumps(page()), encoding="utf-8")
    extraction_path.write_text(json.dumps({"mentions": extraction_mentions()}), encoding="utf-8")
    replay_source = source(source_page_fixture_file=page_path.name, extraction_response_file=extraction_path.name)
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-07-26").collect([replay_source])
    assert result.source_runs[0]["status"] == "collected"
    assert result.source_runs[0]["valid_mention_count"] == 20
    assert len(result.source_mentions) == 20


def test_live_fetch_rejects_prefixed_week_link_as_page_shape_before_dated_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[str] = []

    def fake_urlopen(request: Any, **_kwargs: Any) -> _FakeResponse:
        seen.append(request.full_url)
        return _FakeResponse(
            '<a href="/x/charts/2026-07-26/mostread/nonfiction">prefixed</a>',
            LANDING_URL,
        )

    monkeypatch.setattr("autoscout.source_collectors.urlopen", fake_urlopen)
    with pytest.raises(CollectorRunError) as exc_info:
        fetch_agent_source_page(tmp_path / "config.json", tmp_path, source(), reference_date=WEEK_REFERENCE)
    assert seen == [LANDING_URL]
    assert exc_info.value.debug_metadata["safe_failure_status"] == "page_shape"


def test_live_fetch_skips_malformed_week_date_before_a_later_valid_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[str] = []
    landing_html = (
        '<a href="/charts/2026-99-99/mostread/nonfiction">malformed</a>'
        '<a href="/charts/2026-07-26/mostread/nonfiction">current</a>'
    )

    def fake_urlopen(request: Any, **_kwargs: Any) -> _FakeResponse:
        seen.append(request.full_url)
        if request.full_url == LANDING_URL:
            return _FakeResponse(landing_html, LANDING_URL)
        assert request.full_url == DATED_URL
        return _FakeResponse(CHART_HTML, DATED_URL)

    monkeypatch.setattr("autoscout.source_collectors.urlopen", fake_urlopen)
    fetched, _debug = fetch_agent_source_page(
        tmp_path / "config.json", tmp_path, source(), reference_date=WEEK_REFERENCE
    )
    assert seen == [LANDING_URL, DATED_URL]
    assert fetched["source_page"] == DATED_URL


def test_live_fetch_reports_only_a_source_condition_for_malformed_week_dates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_urlopen(request: Any, **_kwargs: Any) -> _FakeResponse:
        assert request.full_url == LANDING_URL
        return _FakeResponse(
            '<a href="/charts/2026-99-99/mostread/nonfiction">malformed</a>',
            LANDING_URL,
        )

    monkeypatch.setattr("autoscout.source_collectors.urlopen", fake_urlopen)
    with pytest.raises(CollectorRunError) as exc_info:
        fetch_agent_source_page(tmp_path / "config.json", tmp_path, source(), reference_date=WEEK_REFERENCE)
    assert "month must be in" not in str(exc_info.value)
    assert "malformed dated chart link" in str(exc_info.value)
    assert exc_info.value.debug_metadata["safe_failure_status"] == "page_shape"


def test_live_fetch_resolves_landing_page_to_dated_chart(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[str] = []

    def fake_urlopen(request: Any, **_kwargs: Any) -> _FakeResponse:
        seen.append(request.full_url)
        if request.full_url == LANDING_URL:
            return _FakeResponse(LANDING_HTML, LANDING_URL)
        assert request.full_url == DATED_URL
        return _FakeResponse(CHART_HTML, DATED_URL)

    monkeypatch.setattr("autoscout.source_collectors.urlopen", fake_urlopen)
    fetched, debug = fetch_agent_source_page(
        tmp_path / "config.json", tmp_path, source(), reference_date=WEEK_REFERENCE
    )
    assert seen == [LANDING_URL, DATED_URL]
    assert fetched == {
        "source_page": DATED_URL,
        "resolved_url": DATED_URL,
        "reference_date": WEEK_REFERENCE.isoformat(),
        "html": CHART_HTML,
    }
    assert debug["source_url"] == DATED_URL
    assert debug["resolved_url"] == DATED_URL


def test_live_fetch_rejects_dated_redirect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[str] = []

    def fake_urlopen(request: Any, **_kwargs: Any) -> _FakeResponse:
        seen.append(request.full_url)
        if request.full_url == LANDING_URL:
            return _FakeResponse(LANDING_HTML, LANDING_URL)
        assert request.full_url == DATED_URL
        return _FakeResponse(CHART_HTML, DATED_URL + "?ref=redirected")

    monkeypatch.setattr("autoscout.source_collectors.urlopen", fake_urlopen)
    with pytest.raises(CollectorRunError) as exc_info:
        fetch_agent_source_page(tmp_path / "config.json", tmp_path, source(), reference_date=WEEK_REFERENCE)
    assert seen == [LANDING_URL, DATED_URL]
    assert exc_info.value.debug_metadata["safe_failure_status"] == "wrong_target"


def test_live_fetch_rejects_different_week_redirect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[str] = []

    other_week_url = DATED_URL.replace("2026-07-26", "2026-07-19")

    def fake_urlopen(request: Any, **_kwargs: Any) -> _FakeResponse:
        seen.append(request.full_url)
        if request.full_url == LANDING_URL:
            return _FakeResponse(LANDING_HTML, LANDING_URL)
        assert request.full_url == DATED_URL
        return _FakeResponse(CHART_HTML, other_week_url)

    monkeypatch.setattr("autoscout.source_collectors.urlopen", fake_urlopen)
    with pytest.raises(CollectorRunError) as exc_info:
        fetch_agent_source_page(tmp_path / "config.json", tmp_path, source(), reference_date=WEEK_REFERENCE)
    assert seen == [LANDING_URL, DATED_URL]
    assert exc_info.value.debug_metadata["safe_failure_status"] == "wrong_target"
    assert exc_info.value.debug_metadata["resolved_url"] == other_week_url


def test_live_fetch_rejects_landing_redirect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[str] = []

    def fake_urlopen(request: Any, **_kwargs: Any) -> _FakeResponse:
        seen.append(request.full_url)
        return _FakeResponse(LANDING_HTML, "https://evil.example/redirected")

    monkeypatch.setattr("autoscout.source_collectors.urlopen", fake_urlopen)
    with pytest.raises(CollectorRunError) as exc_info:
        fetch_agent_source_page(tmp_path / "config.json", tmp_path, source(), reference_date=WEEK_REFERENCE)
    assert seen == [LANDING_URL]
    assert exc_info.value.debug_metadata["safe_failure_status"] == "wrong_target"


def test_live_fetch_missing_week_link_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[str] = []

    def fake_urlopen(request: Any, **_kwargs: Any) -> _FakeResponse:
        seen.append(request.full_url)
        return _FakeResponse('<a href="/charts/2026-07-26/mostread/fiction">only fiction</a>', LANDING_URL)

    monkeypatch.setattr("autoscout.source_collectors.urlopen", fake_urlopen)
    with pytest.raises(CollectorRunError) as exc_info:
        fetch_agent_source_page(tmp_path / "config.json", tmp_path, source(), reference_date=WEEK_REFERENCE)
    assert seen == [LANDING_URL]
    assert exc_info.value.debug_metadata["safe_failure_status"] == "page_shape"
