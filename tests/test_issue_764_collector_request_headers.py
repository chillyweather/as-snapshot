"""Source page requests name the collector and carry an Accept header (Issue #764)."""

from __future__ import annotations

from typing import Any
from urllib.request import Request

import pytest

import autoscout.source_collectors as collectors_module
from autoscout.source_collectors.agent_fetch import _fetch_bounded_page
from autoscout.source_collectors.base import COLLECTOR_USER_AGENT
from autoscout.source_collectors.base import collector_request_headers


class _StopAfterRequest(Exception):
    pass


def test_the_collector_names_itself_with_a_contact_address_and_not_as_a_browser() -> None:
    headers = collector_request_headers()
    assert headers == {"User-Agent": COLLECTOR_USER_AGENT, "Accept": "*/*"}
    assert COLLECTOR_USER_AGENT.startswith("AutoScout/")
    assert "+https://" in COLLECTOR_USER_AGENT
    for browser_token in ("Mozilla", "Chrome", "Safari", "Gecko", "AppleWebKit"):
        assert browser_token not in COLLECTOR_USER_AGENT


def test_each_call_returns_its_own_headers() -> None:
    first = collector_request_headers()
    first["Content-Type"] = "application/x-www-form-urlencoded"
    assert "Content-Type" not in collector_request_headers()


def test_a_source_page_request_carries_the_collector_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Request] = []

    def recording_urlopen(request: Request, **_kwargs: Any) -> Any:
        seen.append(request)
        raise _StopAfterRequest

    monkeypatch.setattr(collectors_module, "urlopen", recording_urlopen)
    source = {"name": "Probe Source", "url": "https://example.test/list"}
    with pytest.raises(_StopAfterRequest):
        _fetch_bounded_page(source, "https://example.test/list")

    assert len(seen) == 1
    assert seen[0].get_header("User-agent") == COLLECTOR_USER_AGENT
    assert seen[0].get_header("Accept") == "*/*"
