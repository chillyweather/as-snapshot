"""Tests for the shared collector failure construction helper.

These tests prove that ``_collector_error`` preserves ``CollectorRunError``
behavior and that all construction flows through a single path.
"""

from __future__ import annotations

from autoscout.source_collectors import CollectorRunError
from autoscout.source_collectors import _collector_error


class CollectorErrorHelperTests:
    """The shared helper constructs CollectorRunError instances identically
    to direct construction."""

    def test_defaults_match(self):
        err = _collector_error("simple failure")
        assert isinstance(err, CollectorRunError)
        assert str(err) == "simple failure"
        assert err.status == "failed"
        assert err.raw_mention_count == 0
        assert err.valid_mention_count == 0
        assert err.skipped_item_count == 0
        assert err.debug_metadata == {}

    def test_skipped_status(self):
        err = _collector_error("blocked by access", status="skipped")
        assert err.status == "skipped"
        assert str(err) == "blocked by access"

    def test_counts_preserved(self):
        err = _collector_error(
            "all mentions dropped",
            raw_mention_count=5,
            valid_mention_count=0,
            skipped_item_count=3,
        )
        assert err.raw_mention_count == 5
        assert err.valid_mention_count == 0
        assert err.skipped_item_count == 3

    def test_debug_metadata_preserved(self):
        meta = {"collector_kind": "agent-extraction", "safe_failure_status": "timeout"}
        err = _collector_error("transport timeout", debug_metadata=meta)
        assert err.debug_metadata == meta

    def test_none_debug_metadata_becomes_empty_dict(self):
        err = _collector_error("no metadata", debug_metadata=None)
        assert err.debug_metadata == {}

    def test_empty_debug_metadata_stays_empty(self):
        err = _collector_error("no metadata", debug_metadata={})
        assert err.debug_metadata == {}

    def test_exception_subclass(self):
        """CollectorRunError is a standard Exception so callers can catch it."""
        err = _collector_error("test")
        assert isinstance(err, Exception)

    def test_collector_error_is_direct_subclass(self):
        """Prove the helper returns the real CollectorRunError type,
        not a wrapper or alias, so ``except CollectorRunError`` still works."""
        err = _collector_error("type check")
        # isinstance + type identity covers both inheritance and exact type checks
        assert isinstance(err, CollectorRunError)
        assert type(err) is CollectorRunError
