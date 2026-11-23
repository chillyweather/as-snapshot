"""Production-shaped HTTP budget regressions for the grounded stage."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import is_dataclass
from typing import Any

import pytest

from autoscout.grounded_description import OUTCOME_GROUNDED
from autoscout.grounded_description import OUTCOME_SINGLE_SOURCE
from autoscout.grounded_description import REASON_CANDIDATE_BUDGET_EXHAUSTED
from autoscout.grounded_description import REASON_SEARCH_OR_FETCH_FAILURE
from autoscout.grounded_description import BoundedHTTPError
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import CandidateRequestBudget
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import HTTPDiscoveryProvider
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import SynthesisResult
from autoscout.vendor_rate_limit import VendorRateLimiter

CANDIDATE = {
    "candidate_id": "budget book|budget author",
    "identity_key": "budget book|budget author",
    "title": "Budget Book",
    "primary_authors": ["Budget Author"],
    "first_seen_date": "2026-09-14",
}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


class Classifier:
    def classify(self, url: str) -> dict[str, Any]:
        host = url.split("/", 3)[2]
        return {
            "classification": "classified",
            "source_kind": "publisher",
            "source_family": "publisher_copy",
            "owner_id": f"owner-{host}",
            "host": host,
            "path_rule": "/",
            "policy_metadata_version": "test",
            "source_metadata": {},
        }


class DiscoveryTransport:
    def __init__(self, clock: FakeClock, *, advance_seconds: float = 0.0) -> None:
        self.clock = clock
        self.advance_seconds = advance_seconds
        self.calls: list[dict[str, Any]] = []

    def request(self, url: str, **kwargs: Any) -> HTTPResponse:
        self.calls.append({"url": url, **kwargs})
        self.clock.now += self.advance_seconds
        results = [
            {"url": "https://amazon.com/budget", "title": "Budget Book"},
            {"url": "https://review.example.test/budget", "title": "Budget Book"},
            {"url": "https://publisher.example.test/budget", "title": "Budget Book"},
        ]
        return HTTPResponse(url, 200, {}, json.dumps({"results": results}).encode("utf-8"))


REAL_PAGE = (
    "<html><head><title>Budget Book</title>"
    '<meta name="author" content="Budget Author">'
    '<meta property="og:description" content="Книга объясняет управление временем и вниманием."'
    "></head></html>"
)
CHALLENGE_PAGE = "<html><head><title>Amazon.com</title></head><body></body></html>"


class FetchTransport:
    def __init__(self, clock: FakeClock, *, challenge_first: bool = False) -> None:
        self.clock = clock
        self.challenge_first = challenge_first
        self.calls: list[dict[str, Any]] = []

    def request(self, url: str, **kwargs: Any) -> HTTPResponse:
        self.calls.append({"url": url, **kwargs})
        body = CHALLENGE_PAGE if self.challenge_first and len(self.calls) == 1 else REAL_PAGE
        return HTTPResponse(
            url,
            200,
            {"content-type": "text/html; charset=utf-8"},
            body.encode("utf-8"),
        )


class Synthesizer:
    def synthesize(self, candidate: Mapping[str, Any], sources: Any, **_: Any) -> SynthesisResult:
        del candidate, sources
        return SynthesisResult(
            "Оригинальное описание книги о времени и внимании. "
            "Второй фрагмент раскрывает документальную основу, называет проверенные "
            "страницы и уточняет редакционный ракурс книги. "
            "Третье предложение закрепляет вывод и показывает, почему книга попала в обзор."
        )


def _stage(
    clock: FakeClock,
    discovery_transport: DiscoveryTransport,
    fetch_transport: FetchTransport,
    *,
    min_host_interval_seconds: float = 0.0,
) -> GroundedDescriptionStage:
    discovery = HTTPDiscoveryProvider(
        "https://search.example.test/search?q={query}",
        transport=discovery_transport,
        classifier=Classifier(),
    )
    fetch = BoundedHTTPFetcher(
        transport=fetch_transport,
        monotonic=clock.monotonic,
        sleeper=lambda seconds: setattr(clock, "now", clock.now + seconds),
        min_host_interval_seconds=min_host_interval_seconds,
    )
    return GroundedDescriptionStage(
        discovery=discovery,
        fetch=fetch,
        synthesizer=Synthesizer(),
        monotonic=clock.monotonic,
        max_workers=1,
    )


def test_candidate_request_budget_deadline_is_read_only_but_counters_still_increment() -> None:
    clock = FakeClock()
    budget = CandidateRequestBudget(deadline=90.0, max_fetches=1, monotonic=clock.monotonic)

    with pytest.raises(AttributeError):
        budget.deadline = 91.0  # type: ignore[misc]

    budget.before_request("fetch", 10.0)

    assert not is_dataclass(budget)
    representation = repr(budget)
    assert "deadline=90.0" in representation
    assert "max_fetches=1" in representation
    assert "monotonic=" in representation
    assert "discovery_requests=0" in representation
    assert "fetch_requests=1" in representation
    assert budget.fetch_requests == 1
    assert budget.discovery_requests == 0


def test_exhausted_fetch_budget_does_not_wait_or_make_a_fourth_request() -> None:
    clock = FakeClock()
    transport = FetchTransport(clock)
    sleeps: list[float] = []
    fetcher = BoundedHTTPFetcher(
        transport=transport,
        monotonic=clock.monotonic,
        sleeper=lambda seconds: (sleeps.append(seconds), setattr(clock, "now", clock.now + seconds)),
        min_host_interval_seconds=10.0,
        challenge_attempts=1,
    )
    budget = CandidateRequestBudget(deadline=90.0, max_fetches=3, monotonic=clock.monotonic)
    result = {
        "url": "https://publisher.example.test/budget",
        "source_kind": "publisher",
        "source_family": "publisher_copy",
        "owner_id": "publisher-owner",
        "classification": "classified",
        "title": "Budget Book",
        "primary_authors": ["Budget Author"],
    }

    for _ in range(3):
        fetcher.fetch(result, request_budget=budget)

    with pytest.raises(BoundedHTTPError, match="fetch request budget exhausted"):
        fetcher.fetch(result, request_budget=budget)

    assert len(transport.calls) == 3
    assert sleeps == [10.0, 10.0]
    assert budget.fetch_requests == 3


def test_real_stage_allows_only_three_fetch_requests_including_challenge_retry() -> None:
    clock = FakeClock()
    discovery_transport = DiscoveryTransport(clock)
    fetch_transport = FetchTransport(clock, challenge_first=True)
    artifact = _stage(clock, discovery_transport, fetch_transport).run(
        [CANDIDATE],
        operation_id="budget-test",
        operation_date="2026-09-14",
        force_candidates=True,
    )

    assert len(discovery_transport.calls) == 1
    assert len(fetch_transport.calls) == 3
    assert artifact.outcomes[0].outcome in {OUTCOME_GROUNDED, OUTCOME_SINGLE_SOURCE}


def test_discovery_overrun_does_not_reuse_full_stage_budget_for_fetch() -> None:
    clock = FakeClock()
    discovery_transport = DiscoveryTransport(clock, advance_seconds=85.0)
    fetch_transport = FetchTransport(clock)
    artifact = _stage(clock, discovery_transport, fetch_transport).run(
        [CANDIDATE],
        operation_id="discovery-budget",
        operation_date="2026-09-14",
        force_candidates=True,
    )

    assert discovery_transport.calls[0]["timeout_seconds"] == 10.0
    assert not fetch_transport.calls
    assert artifact.outcomes[0].reason_code == REASON_CANDIDATE_BUDGET_EXHAUSTED


def test_vendor_rate_limit_wait_is_outside_candidate_timing_and_discovery_deadline() -> None:
    """A shared vendor wait consumes stage time, not candidate discovery time."""

    clock = FakeClock()

    class RateLimitedTransport:
        def __init__(self) -> None:
            self.started_at: list[float] = []
            self.calls: list[dict[str, Any]] = []
            self.advance_seconds = iter((0.5, 1.5))

        def request(self, url: str, **kwargs: Any) -> HTTPResponse:
            self.started_at.append(clock.now)
            self.calls.append({"url": url, **kwargs})
            clock.now += next(self.advance_seconds)
            return HTTPResponse(url, 200, {}, b'{"results": []}')

    transport = RateLimitedTransport()
    limiter = VendorRateLimiter(
        min_interval_seconds=1.0,
        monotonic=clock.monotonic,
        sleeper=lambda seconds: setattr(clock, "now", clock.now + seconds),
    )
    discovery = HTTPDiscoveryProvider(
        "https://search.example.test/search?q={query}",
        transport=transport,
        classifier=Classifier(),
        rate_limiter=limiter,
    )
    second_candidate = {
        **CANDIDATE,
        "candidate_id": "second budget book|budget author",
        "identity_key": "second budget book|budget author",
        "title": "Second Budget Book",
    }

    artifact = GroundedDescriptionStage(
        discovery=discovery,
        rate_limiter=limiter,
        monotonic=clock.monotonic,
        stage_budget_seconds=10.0,
        discovery_budget_seconds=2.0,
        candidate_envelope_seconds=2.0,
        # This test is about how a queued vendor wait is accounted for, not
        # about concurrency. The fake clock and the fake transport's advance
        # sequence are plain shared objects, so at the default worker count
        # two candidates race for them and the per-candidate seconds below
        # swap: the assertions failed about one run in four. One worker
        # makes the sequence the assertions describe the only one possible.
        max_workers=1,
    ).run(
        [CANDIDATE, second_candidate],
        operation_id="vendor-queue-budget",
        operation_date="2026-09-14",
        force_candidates=True,
    )

    # The second request waits from 0.5 to 1.0 before candidate timing starts.
    assert transport.started_at == [0.0, 1.0]
    assert [call["timeout_seconds"] for call in transport.calls] == [2.0, 2.0]
    assert clock.now == 2.5
    assert artifact.elapsed_seconds == 2.5
    assert [outcome.reason_code for outcome in artifact.outcomes] == ["no_results", "no_results"]
    assert artifact.outcomes[0].candidate_timing.to_dict() == {
        "discovery_seconds": 0.5,
        "fetch_seconds": None,
        "synthesis_seconds": None,
        "total_seconds": 0.5,
    }
    assert artifact.outcomes[1].candidate_timing.to_dict() == {
        "discovery_seconds": 1.5,
        "fetch_seconds": None,
        "synthesis_seconds": None,
        "total_seconds": 1.5,
    }


def test_vendor_queue_wait_that_cannot_fit_refuses_before_discovery() -> None:
    clock = FakeClock()
    transport = DiscoveryTransport(clock)
    sleeps: list[float] = []
    # The vendor wait needs 30 s, so it cannot fit a 10 s stage deadline even
    # though the 10 s budget passes the measured admission gate (issue #633).
    limiter = VendorRateLimiter(
        min_interval_seconds=30.0,
        monotonic=clock.monotonic,
        sleeper=lambda seconds: sleeps.append(seconds),
    )
    limiter.wait()
    discovery = HTTPDiscoveryProvider(
        "https://search.example.test/search?q={query}",
        transport=transport,
        classifier=Classifier(),
        rate_limiter=limiter,
    )

    artifact = GroundedDescriptionStage(
        discovery=discovery,
        rate_limiter=limiter,
        monotonic=clock.monotonic,
        stage_budget_seconds=10.0,
        discovery_budget_seconds=0.25,
        candidate_envelope_seconds=0.25,
        max_workers=1,
    ).run(
        [CANDIDATE],
        operation_id="vendor-queue-refused",
        operation_date="2026-09-14",
        force_candidates=True,
    )

    assert transport.calls == []
    assert sleeps == []
    outcome = artifact.outcomes[0]
    assert outcome.reason_code == REASON_CANDIDATE_BUDGET_EXHAUSTED
    assert "vendor queue" in outcome.reason_detail
    assert "candidate work did not start" in outcome.reason_detail
    assert "timing: discovery=unknown; fetch=unknown; synthesis=unknown; total=unknown" in outcome.reason_detail
    assert outcome.candidate_timing.to_dict() == {
        "discovery_seconds": None,
        "fetch_seconds": None,
        "synthesis_seconds": None,
        "total_seconds": None,
    }


def test_expired_stage_deadline_does_not_invoke_vendor_limiter() -> None:
    clock = FakeClock()
    calls: list[dict[str, Any]] = []

    class RecordingLimiter:
        def wait(self, **kwargs: Any) -> float:
            calls.append(kwargs)
            return 0.0

    stage = GroundedDescriptionStage(
        rate_limiter=RecordingLimiter(),  # type: ignore[arg-type]
        monotonic=clock.monotonic,
    )

    assert stage._wait_for_discovery_capacity(0.0) is False
    assert calls == []


def test_vendor_rate_limiter_rejects_a_sleeper_overshoot_after_no_vendor_call() -> None:
    clock = FakeClock()
    sleeps: list[float] = []

    def overshooting_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.now += seconds + 2.0

    limiter = VendorRateLimiter(
        min_interval_seconds=1.0,
        monotonic=clock.monotonic,
        sleeper=overshooting_sleep,
    )
    limiter.wait()

    assert limiter.wait(deadline=2.0) is None
    assert sleeps == [1.0]
    assert clock.now == 3.0


def test_discovery_exception_reason_code_is_read_only_for_bounded_http_errors() -> None:
    class DuckTypedBudgetError(RuntimeError):
        reason_code = REASON_CANDIDATE_BUDGET_EXHAUSTED

    class FailingDiscovery:
        def discover(self, _candidate: Mapping[str, Any], **_: Any) -> list[Any]:
            raise DuckTypedBudgetError("unrelated exception with a similar attribute")

    artifact = GroundedDescriptionStage(
        discovery=FailingDiscovery(),
        monotonic=FakeClock().monotonic,
        max_workers=1,
    ).run([CANDIDATE], operation_id="typed-reason", operation_date="2026-09-14", force_candidates=True)

    assert artifact.outcomes[0].reason_code == REASON_SEARCH_OR_FETCH_FAILURE


def test_fetch_exception_with_non_string_reason_code_fails_closed() -> None:
    class Discovery:
        def discover(self, _candidate: Mapping[str, Any], **_: Any) -> list[dict[str, Any]]:
            return [
                {
                    "url": "https://publisher.example.test/budget",
                    "source_kind": "publisher",
                    "source_family": "publisher_copy",
                    "owner_id": "publisher-owner",
                    "classification": "classified",
                }
            ]

    class Fetch:
        def fetch(self, _result: Any, **_: Any) -> dict[str, Any]:
            raise BoundedHTTPError("malformed reason", reason_code=None)  # type: ignore[arg-type]

    clock = FakeClock()
    artifact = GroundedDescriptionStage(
        discovery=Discovery(),
        fetch=Fetch(),
        monotonic=clock.monotonic,
        max_workers=1,
    ).run([CANDIDATE], operation_id="malformed-reason", operation_date="2026-09-14", force_candidates=True)

    assert artifact.outcomes[0].reason_code == REASON_SEARCH_OR_FETCH_FAILURE


def test_discovery_and_post_discovery_budgets_have_separate_deadlines() -> None:
    clock = FakeClock()

    class CapturingDiscovery:
        def __init__(self) -> None:
            self.budget: CandidateRequestBudget | None = None

        def discover(
            self,
            _candidate: Mapping[str, Any],
            *,
            request_budget: CandidateRequestBudget,
            **_: Any,
        ) -> list[dict[str, Any]]:
            self.budget = request_budget
            return [
                {
                    "url": "https://publisher.example.test/budget",
                    "source_kind": "publisher",
                    "source_family": "publisher_copy",
                    "owner_id": "publisher-owner",
                    "classification": "classified",
                }
            ]

    class CapturingFetch:
        def __init__(self) -> None:
            self.budget: CandidateRequestBudget | None = None

        def fetch(self, result: Any, *, request_budget: CandidateRequestBudget, **_: Any) -> dict[str, Any]:
            self.budget = request_budget
            url = result.url if hasattr(result, "url") else str(result["url"])
            return {
                "url": str(url),
                "source_kind": "publisher",
                "source_family": "publisher_copy",
                "owner_id": "publisher-owner",
                "source_language": "en",
                "title": "Budget Book",
                "primary_authors": ["Budget Author"],
                "subject_text": "A book about time and attention.",
                "checked_date": "2026-09-14",
                "classification": "classified",
            }

    discovery = CapturingDiscovery()
    fetch = CapturingFetch()
    artifact = GroundedDescriptionStage(
        discovery=discovery,
        fetch=fetch,
        synthesizer=Synthesizer(),
        monotonic=clock.monotonic,
        stage_budget_seconds=120.0,
        discovery_budget_seconds=10.0,
        candidate_envelope_seconds=90.0,
        max_workers=1,
    ).run([CANDIDATE], operation_id="separate-budgets", operation_date="2026-09-14", force_candidates=True)

    assert artifact.outcomes[0].outcome == OUTCOME_SINGLE_SOURCE
    assert discovery.budget is not None
    assert fetch.budget is not None
    assert discovery.budget is not fetch.budget
    assert discovery.budget.deadline == 10.0
    assert fetch.budget.deadline == 90.0


def test_stage_admission_reserves_discovery_and_candidate_envelopes() -> None:
    clock = FakeClock()
    discovery_transport = DiscoveryTransport(clock)
    fetch_transport = FetchTransport(clock)
    artifact = _stage(clock, discovery_transport, fetch_transport).run(
        [CANDIDATE],
        operation_id="admission-budget",
        operation_date="2026-09-14",
        force_candidates=True,
    )

    # The default 900-second stage is sufficient for one measured
    # per-candidate reservation. A stage whose worker-seconds cannot hold one
    # measured candidate must not start discovery at all (issue #633 admits
    # on the measured cost, not the fixed envelope).
    short_discovery = DiscoveryTransport(clock)
    short = GroundedDescriptionStage(
        discovery=short_discovery,
        monotonic=clock.monotonic,
        stage_budget_seconds=2.0,
        discovery_budget_seconds=10.0,
        candidate_envelope_seconds=90.0,
    ).run([CANDIDATE], operation_id="short-admission", operation_date="2026-09-14", force_candidates=True)

    assert artifact.outcomes[0].reason_code != REASON_CANDIDATE_BUDGET_EXHAUSTED
    assert short.outcomes[0].outcome == "not_attempted"
    assert short.outcomes[0].reason_code == "not_attempted_stage_budget"
    assert not short_discovery.calls


def test_host_rate_limit_wait_does_not_start_when_deadline_cannot_hold_it() -> None:
    clock = FakeClock()
    discovery_transport = DiscoveryTransport(clock)
    fetch_transport = FetchTransport(clock, challenge_first=True)
    original_request = fetch_transport.request

    def leave_only_one_second(url: str, **kwargs: Any) -> HTTPResponse:
        response = original_request(url, **kwargs)
        if len(fetch_transport.calls) == 1:
            clock.now = 89.0
        return response

    fetch_transport.request = leave_only_one_second  # type: ignore[method-assign]
    artifact = _stage(
        clock,
        discovery_transport,
        fetch_transport,
        min_host_interval_seconds=100.0,
    ).run(
        [CANDIDATE],
        operation_id="host-wait-deadline",
        operation_date="2026-09-14",
        force_candidates=True,
    )

    assert len(fetch_transport.calls) == 1
    assert clock.now == 89.0
    assert artifact.outcomes[0].reason_code == REASON_CANDIDATE_BUDGET_EXHAUSTED


def test_deadline_expiry_between_challenge_attempts_makes_no_second_request() -> None:
    clock = FakeClock()
    discovery_transport = DiscoveryTransport(clock)
    fetch_transport = FetchTransport(clock, challenge_first=True)
    original_request = fetch_transport.request

    def expire_after_first(url: str, **kwargs: Any) -> HTTPResponse:
        response = original_request(url, **kwargs)
        if len(fetch_transport.calls) == 1:
            clock.now = 90.0
        return response

    fetch_transport.request = expire_after_first  # type: ignore[method-assign]
    artifact = _stage(clock, discovery_transport, fetch_transport).run(
        [CANDIDATE],
        operation_id="challenge-deadline",
        operation_date="2026-09-14",
        force_candidates=True,
    )

    assert len(fetch_transport.calls) == 1
    assert artifact.outcomes[0].reason_code == REASON_CANDIDATE_BUDGET_EXHAUSTED
