"""Shared weekly source-processing runtime defaults.

The extraction constants separate inactivity detection from absolute safety and
portfolio scheduling.
"""

from collections.abc import Iterable
from collections.abc import Mapping
from typing import Any

from autoscout.ranking_detail_route import RankingDetailRoute
from autoscout.ranking_identity import ranking_identity_declared
from autoscout.ranking_request import ranking_request_from_source

CAPTURE_RUNTIME_BUDGET_SECONDS = 150
CAPTURE_SOURCE_TIMEOUT_SECONDS = 10
# A source whose one capture attempt timed out, or was rate-limited with a
# Retry-After inside the stage budget, gets exactly one more attempt with this
# socket ceiling (issue #1005). The stage budget and finalization reserve are unchanged.
CAPTURE_SOURCE_RETRY_TIMEOUT_SECONDS = 30
# One bounded detail page costs at most this much of the capture stage budget.
# The measured worst case on 2026-08-24 was 2.62s per product page over 100
# pages, so this is an evidence-backed allowance rather than a guess.
CAPTURE_DETAIL_PAGE_ALLOWANCE_SECONDS = 3.0
PORTFOLIO_RUNTIME_BUDGET_SECONDS = 1800
CODEX_TIMEOUT_SECONDS = 180
# One bounded scheduling segment admits work for at most one portfolio budget.
SEGMENT_BUDGET_SECONDS = PORTFOLIO_RUNTIME_BUDGET_SECONDS
# One subprocess has this absolute emergency ceiling, regardless of progress.
CODEX_ABSOLUTE_CEILING_SECONDS = PORTFOLIO_RUNTIME_BUDGET_SECONDS
# Recognized structured Codex events renew this inactivity lease.
CODEX_NO_PROGRESS_TIMEOUT_SECONDS = CODEX_TIMEOUT_SECONDS
# One configured weekly operation may spend at most two hours in total.
WEEKLY_OPERATION_CEILING_SECONDS = 7200
# The Kirkus pass the weekly run starts before capture may spend at most this
# long (issue #816). A pass of the onboarded declaration is at most 80 requests
# twenty seconds apart, about half an hour; a pass that reaches this allowance
# stops and the next one continues, so the stages after it keep their time.
KIRKUS_PASS_RUNTIME_BUDGET_SECONDS = 2400
# The PM deals pass the weekly run starts before capture may spend at most this
# long (issue #946). A pass reads about a week of daily emails, a few seconds of
# IMAP; at this allowance it fetches no further message and keeps what it read.
PM_PASS_RUNTIME_BUDGET_SECONDS = 300
# The PW issue pass the weekly run starts before capture may start no new issue
# after this long (issue #956). One issue is a 90-270 MB PDF and a minute or two
# of parsing; an issue already started finishes, bounded by its socket timeouts.
PW_PASS_RUNTIME_BUDGET_SECONDS = 900
# Reserve this fixed time for finalization and recovery after extraction.
FINALIZATION_RESERVE_SECONDS = 600
# Production retries must retain this exact portfolio headroom.
RETRY_HEADROOM_SECONDS = 180.0


def declared_detail_page_allowance(source: Mapping[str, Any]) -> float:
    """Return what one source's declared detail route is allowed to cost.

    One rule, stated once.  The stage budget and the per-source allowance both
    grow by a declared route's pages at the same measured per-page allowance,
    and computing that surcharge in one place is what keeps the two bounds
    from drifting apart.  A source without a declared route costs nothing
    extra.
    """
    route = RankingDetailRoute.from_source(source)
    if route is None:
        return 0.0
    return route.max_pages * CAPTURE_DETAIL_PAGE_ALLOWANCE_SECONDS


def declared_extra_request_count(source: Mapping[str, Any]) -> int:
    """Return how many page fetches past the first one source declares.

    Capture holds a source to one wall clock across every request it makes,
    so every declared request shape has to be paid for out of that clock.
    Four declarations fetch whole pages beyond the first, and each is read
    from the declaration that causes it: a bounded paging sequence walks its
    declared pages, an identity route adds one plain GET, an Amazon Charts
    source reads the landing page before the dated chart, and a Perlentaucher
    digest reads one dated page per day after its index.  A declared
    detail route is counted separately, at its own measured per-page
    allowance.

    Counting them here is what keeps a source that declares its pages under
    a key other than the detail route from failing closed the first week its
    pages are slow (issue #717 review).
    """
    request = ranking_request_from_source(source)
    extra = request.max_pages - 1 if request.is_paged else 0
    if ranking_identity_declared(source):
        extra += 1
    if source.get("amazon_charts_weekly") is True:
        extra += 1
    daily_pages = source.get("perlentaucher_max_daily_pages")
    if str(source.get("contract_family", "")).strip().casefold() == "perlentaucher-daily-digest" and (
        type(daily_pages) is int and daily_pages > 0
    ):
        extra += daily_pages
    return extra


def declared_request_allowance(source: Mapping[str, Any], request_allowance_seconds: float) -> float:
    """Return what one source's declarations cost beyond a single page fetch.

    One rule, stated once.  Every declared page fetch past the first costs
    the allowance one bounded request is held to, and a declared detail route
    costs its own measured per-page allowance on top; the stage budget and
    the per-source allowance both grow by this same sum, which is what keeps
    the two bounds from drifting apart.
    """
    extra_requests = declared_extra_request_count(source) * request_allowance_seconds
    return extra_requests + declared_detail_page_allowance(source)


def capture_source_allowance_for(source: Mapping[str, Any], socket_timeout_seconds: float) -> float:
    """Return the wall-clock allowance one source's capture may spend.

    The clamped per-source timeout covers a source that fetches one page.
    Every page a source declares beyond that first one - a paging sequence,
    an identity route, an Amazon Charts landing page - is a further bounded
    request held to the same clamped timeout, so the allowance grows by one
    such timeout for each of them.  A declared detail route fetches one page
    per row up to its declared cap and grows the allowance by that
    declaration at the measured per-page allowance the stage budget uses.  A
    100-page detail route is therefore bounded by 310 seconds rather than by
    10, a four-page ranking that also declares an identity route by 50, and a
    source that declares one page keeps precisely the bound it had.

    Raising a declared route's bound above the flat per-source ceiling is a
    deliberate product decision taken in review of issue #717, whose original
    text kept that ceiling out of scope and accepted that a legitimate
    100-page source would fail closed every week instead.

    Like the stage budget, this is a pure function of configuration: it reads
    no clock and makes no request.  The caller still clamps the result to the
    stage budget it has left, so a late source cannot overrun the stage.
    """
    return socket_timeout_seconds + declared_request_allowance(source, socket_timeout_seconds)


def capture_runtime_budget_for(sources: Iterable[Mapping[str, Any]]) -> float:
    """Return the capture-stage budget the configured portfolio actually needs.

    The base budget assumes one fetch per source.  A source that declares
    more fetches than that - further ranking pages, an identity route, an
    Amazon Charts landing page, a detail route walked one page per row -
    grows the stage budget by exactly what it declares, at the per-request
    ceiling those fetches are each held to.  A source that declares one page
    adds nothing, which keeps the bound tight for the single-page sources
    that make up most of a portfolio.

    The result is a pure function of configuration.  It reads no clock and
    makes no request, so a run's budget is knowable before the run starts.
    """
    declared_seconds = sum(declared_request_allowance(source, CAPTURE_SOURCE_TIMEOUT_SECONDS) for source in sources)
    return CAPTURE_RUNTIME_BUDGET_SECONDS + declared_seconds
