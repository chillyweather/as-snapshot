"""Registered source collectors, split by collector family (issue #310).

This package preserves the public surface of the former single-module
``autoscout/source_collectors.py``: every name importable from
``autoscout.source_collectors`` before the split is re-exported here, and
``COLLECTOR_REGISTRY`` remains the single dispatch vocabulary.

Patch seams: tests monkeypatch attributes on this package (``urlopen``,
``fetch_agent_source_page``, ``live_agent_extraction_response``,
``REAL_AGENT_EXTRACTION_TRANSPORT``). Internal call sites resolve those
names through the package at call time (see ``base.collectors_pkg``), so
patching the package attribute keeps working exactly as it did on the
single module.
"""

# ruff: noqa: F401
from __future__ import annotations

from urllib.request import urlopen

from autoscout.source_collectors.agent_backends import REAL_AGENT_EXTRACTION_TRANSPORT
from autoscout.source_collectors.agent_backends import AgentExtractionBackend
from autoscout.source_collectors.agent_backends import AgentExtractionTransport
from autoscout.source_collectors.agent_backends import FakeAgentExtractionBackend
from autoscout.source_collectors.agent_backends import FixtureAgentExtractionBackend
from autoscout.source_collectors.agent_backends import OpenAIChatCompletionAgentExtractionTransport
from autoscout.source_collectors.agent_backends import RealAgentExtractionBackend
from autoscout.source_collectors.agent_backends import agent_backend_debug
from autoscout.source_collectors.agent_backends import configured_extraction_response
from autoscout.source_collectors.agent_backends import fake_agent_extraction_response
from autoscout.source_collectors.agent_backends import live_agent_extraction_response
from autoscout.source_collectors.agent_backends import openai_chat_completion_content
from autoscout.source_collectors.agent_backends import select_agent_extraction_backend
from autoscout.source_collectors.agent_extraction import AgentExtractionCollector
from autoscout.source_collectors.agent_extraction import agent_extraction_capture_mode
from autoscout.source_collectors.agent_extraction import configured_agent_extraction_inputs
from autoscout.source_collectors.agent_extraction import configured_source_page
from autoscout.source_collectors.agent_extraction import live_agent_extraction_inputs
from autoscout.source_collectors.agent_fetch import _MAX_AGENT_PAGE_BYTES
from autoscout.source_collectors.agent_fetch import _bounded_response_text
from autoscout.source_collectors.agent_fetch import fetch_agent_source_page
from autoscout.source_collectors.agent_mentions import agent_extraction_mentions_from_response
from autoscout.source_collectors.agent_model_input import compact_model_html
from autoscout.source_collectors.agent_model_input import model_bounded_page_data
from autoscout.source_collectors.base import _AUDIOBOOK_EXCLUDED_TERMS
from autoscout.source_collectors.base import _AUDIOBOOK_UNSUPPORTED_CLAIMS
from autoscout.source_collectors.base import _JSON_URL_FIELDS
from autoscout.source_collectors.base import _JSON_URL_PATTERN
from autoscout.source_collectors.base import CONTRACT_FAMILIES
from autoscout.source_collectors.base import DEFAULT_MODEL_MAX_OUTPUT_TOKENS
from autoscout.source_collectors.base import FORBIDDEN_COLLECTOR_RESPONSE_FIELDS
from autoscout.source_collectors.base import NON_ENGLISH_STRICT_CHART_SEARCH_TRANSLATION_RULE
from autoscout.source_collectors.base import Collector
from autoscout.source_collectors.base import CollectorResult
from autoscout.source_collectors.base import CollectorRunError
from autoscout.source_collectors.base import _collector_error
from autoscout.source_collectors.base import _json_field_urls
from autoscout.source_collectors.base import _json_unescape
from autoscout.source_collectors.base import _normalize_hostname
from autoscout.source_collectors.base import _swap_www_host
from autoscout.source_collectors.base import _validate_exact_book_list_mention
from autoscout.source_collectors.base import author_from_card
from autoscout.source_collectors.base import canonical_provenance_url
from autoscout.source_collectors.base import capture_extraction_response
from autoscout.source_collectors.base import capture_live_response
from autoscout.source_collectors.base import capture_normalized_mentions
from autoscout.source_collectors.base import clean_text
from autoscout.source_collectors.base import codex_provenance_links
from autoscout.source_collectors.base import collectors_pkg
from autoscout.source_collectors.base import contract_family
from autoscout.source_collectors.base import evidence_url_verified
from autoscout.source_collectors.base import extraction_contract_violation
from autoscout.source_collectors.base import extraction_entries
from autoscout.source_collectors.base import likely_blocked_response
from autoscout.source_collectors.base import normalize_chart_evidence_url
from autoscout.source_collectors.base import normalize_codex_provenance_url
from autoscout.source_collectors.base import normalized_agent_mention
from autoscout.source_collectors.base import normalized_candidate_category
from autoscout.source_collectors.base import output_path
from autoscout.source_collectors.base import page_links
from autoscout.source_collectors.base import resolve_config_path
from autoscout.source_collectors.base import resolve_provenance_url
from autoscout.source_collectors.base import same_domain
from autoscout.source_collectors.base import source_page_url
from autoscout.source_collectors.base import validate_extraction_page_identity
from autoscout.source_collectors.bookpage import BOOKPAGE_FAILURE_STATUSES
from autoscout.source_collectors.bookpage import BookPageCollector
from autoscout.source_collectors.bookpage import absolute_bookpage_url
from autoscout.source_collectors.bookpage import bookpage_entries
from autoscout.source_collectors.bookpage import bookpage_mentions_from_response
from autoscout.source_collectors.bookpage import configured_bookpage_response
from autoscout.source_collectors.bookpage import entries_from_links
from autoscout.source_collectors.bookpage import live_bookpage_response
from autoscout.source_collectors.bookpage import parse_bookpage_html
from autoscout.source_collectors.codex_extraction import CODEX_CANONICAL_MISSING_FIELDS
from autoscout.source_collectors.codex_extraction import CodexExtractionCollector
from autoscout.source_collectors.codex_extraction import MissingRequiredFieldsAggregate
from autoscout.source_collectors.codex_extraction import _codex_drop_failure_status
from autoscout.source_collectors.codex_extraction import _validate_codex_raw_required_fields
from autoscout.source_collectors.codex_extraction import authoritative_empty_codex_artifact
from autoscout.source_collectors.codex_extraction import codex_extraction_collector_result
from autoscout.source_collectors.codex_extraction import codex_missing_field_aggregate
from autoscout.source_collectors.codex_extraction import configured_codex_extraction_inputs
from autoscout.source_collectors.codex_extraction import resolve_codex_artifact_path
from autoscout.source_collectors.contract import EVIDENCE_URL_GROUNDING_RULE
from autoscout.source_collectors.contract import agent_extraction_prompt
from autoscout.source_collectors.contract import extraction_contract
from autoscout.source_collectors.nonfiction_fr import NonfictionFrCollector
from autoscout.source_collectors.nonfiction_fr import _fetch_nonfiction_fr_document
from autoscout.source_collectors.nonfiction_fr import _nonfiction_fr_rss_item_text
from autoscout.source_collectors.nonfiction_fr import configured_nonfiction_fr_inputs
from autoscout.source_collectors.nonfiction_fr import live_nonfiction_fr_review_page
from autoscout.source_collectors.nonfiction_fr import live_nonfiction_fr_rss
from autoscout.source_collectors.nonfiction_fr import nonfiction_fr_book_blocks
from autoscout.source_collectors.nonfiction_fr import nonfiction_fr_max_new_items
from autoscout.source_collectors.nonfiction_fr import nonfiction_fr_review_date
from autoscout.source_collectors.nonfiction_fr import nonfiction_fr_rss_entries
from autoscout.source_collectors.nonfiction_fr import nonfiction_fr_seen_guids
from autoscout.source_collectors.nonfiction_fr import persist_nonfiction_fr_seen_guids
from autoscout.source_collectors.npr import NPR_FAILURE_STATUSES
from autoscout.source_collectors.npr import NPRCollector
from autoscout.source_collectors.npr import absolute_npr_url
from autoscout.source_collectors.npr import configured_npr_response
from autoscout.source_collectors.npr import live_npr_response
from autoscout.source_collectors.npr import npr_entries
from autoscout.source_collectors.npr import npr_item_url_from_card
from autoscout.source_collectors.npr import npr_mentions_from_response
from autoscout.source_collectors.npr import npr_title_from_card
from autoscout.source_collectors.npr import parse_npr_html
from autoscout.source_collectors.pm_deal import PmDealCollector
from autoscout.source_collectors.pw_issue import PwIssueCollector

COLLECTOR_REGISTRY: dict[str, Collector] = {
    "bookpage-recorded-response": BookPageCollector(variant="recorded-response"),
    "bookpage-source-page": BookPageCollector(variant="source-page"),
    "bookpage-live": BookPageCollector(variant="live"),
    "npr-source-page": NPRCollector(variant="source-page"),
    "npr-live": NPRCollector(variant="live"),
    "nonfiction-fr-rss-replay": NonfictionFrCollector(variant="replay"),
    "nonfiction-fr-rss-live": NonfictionFrCollector(variant="live"),
    "agent-extraction": AgentExtractionCollector(variant="configured"),
    "agent-extraction-replay": AgentExtractionCollector(variant="configured"),
    "agent-extraction-live": AgentExtractionCollector(variant="live"),
    "codex-extraction-replay": CodexExtractionCollector(),
    "pm-deal-email-live": PmDealCollector(variant="live"),
    "pm-deal-email-replay": PmDealCollector(variant="replay"),
    "pw-issue-pdf-live": PwIssueCollector(variant="live"),
    "pw-issue-pdf-replay": PwIssueCollector(variant="replay"),
}


def registered_extraction_methods() -> list[str]:
    """Return the supported ``extraction_method`` values, sorted.

    Discoverable from the registry so a future collector registration
    automatically becomes recognized here -- no other code needs to change
    to keep this vocabulary current. Used by the dispatcher in
    ``source_candidates()`` to name the valid vocabulary when a source's
    ``extraction_method`` matches no registered collector."""
    return sorted(COLLECTOR_REGISTRY)
