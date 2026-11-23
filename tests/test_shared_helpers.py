"""Tests for the centralized source-rules, identity, and JSON I/O helpers.

These tests prove that the shared helper implementations preserve behavior
through the Source Mention / Scouting Candidate path, and that callers
using the shared imports produce the same results as before.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from autoscout.identity import normalize_identity_text
from autoscout.json_io import read_json
from autoscout.json_io import read_json_or_default
from autoscout.json_io import write_json
from autoscout.source_rules import DISCOVERY_SOURCE_FAMILIES
from autoscout.source_rules import SKIPPED_ACCESS_STATUSES
from autoscout.source_rules import can_create_source_mention
from autoscout.source_rules import is_active_source
from autoscout.source_rules import is_discovery_source
from autoscout.source_rules import is_skipped_source
from autoscout.source_rules import skipped_reason

# ---------------------------------------------------------------------------
# SKIPPED_ACCESS_STATUSES / skipped_reason
# ---------------------------------------------------------------------------


class SkippedSourceRulesTests:
    def test_skipped_access_statuses_are_the_canonical_set(self):
        assert "paid" in SKIPPED_ACCESS_STATUSES
        assert "blocked" in SKIPPED_ACCESS_STATUSES
        assert "fragile" in SKIPPED_ACCESS_STATUSES
        assert "unavailable" in SKIPPED_ACCESS_STATUSES
        assert "manual import" in SKIPPED_ACCESS_STATUSES
        assert "unsuitable" in SKIPPED_ACCESS_STATUSES

    def test_is_skipped_source_matches_access_status(self):
        assert is_skipped_source({"access_status": "paid"})
        assert is_skipped_source({"access_status": "BLOCKED"})  # case-insensitive
        assert is_skipped_source({"access_status": "  fragile  "})  # whitespace
        assert not is_skipped_source({"access_status": "open"})
        assert not is_skipped_source({"access_status": ""})

    def test_skipped_reason_includes_notes(self):
        source = {"access_status": "paid", "notes": "paywall confirmed"}
        assert skipped_reason(source) == "paid: paywall confirmed"

    def test_skipped_reason_no_notes(self):
        assert skipped_reason({"access_status": "blocked"}) == "blocked"

    def test_skipped_reason_default_status(self):
        assert skipped_reason({}) == "unsuitable"


# ---------------------------------------------------------------------------
# is_active_source
# ---------------------------------------------------------------------------


class ActiveSourceRulesTests:
    def test_active_true_by_default(self):
        assert is_active_source({})
        assert is_active_source({"active": True})

    def test_inactive_when_explicitly_false(self):
        assert not is_active_source({"active": False})
        # Non-False values (including the string "False") keep the source active
        assert is_active_source({"active": "False"})
        assert is_active_source({"active": True})


# ---------------------------------------------------------------------------
# DISCOVERY_SOURCE_FAMILIES / is_discovery_source
# ---------------------------------------------------------------------------


class DiscoverySourceRulesTests:
    def test_discovery_families_are_the_canonical_set(self):
        assert "award" in DISCOVERY_SOURCE_FAMILIES
        assert "bestseller/ranking" in DISCOVERY_SOURCE_FAMILIES
        assert "media/curated list" in DISCOVERY_SOURCE_FAMILIES
        assert "reader-interest" in DISCOVERY_SOURCE_FAMILIES
        assert "audiobook ranking" in DISCOVERY_SOURCE_FAMILIES
        assert "rights/deal" in DISCOVERY_SOURCE_FAMILIES

    def test_is_discovery_source_matches_family(self):
        assert is_discovery_source({"family": "award"})
        assert is_discovery_source({"family": "  Media/Curated List  "})
        assert not is_discovery_source({"family": "unknown"})
        assert not is_discovery_source({})

    def test_discovery_source_allows_source_mention_pipeline_to_proceed(self):
        """Integration seam: a source with a discovery family passes the
        eligible-source gate in SourceCollection.collect()."""
        assert is_discovery_source({"family": "reader-interest"})
        # Non-discovery sources are ignored by the pipeline
        assert not is_discovery_source({"family": "manual source"})


# ---------------------------------------------------------------------------
# can_create_source_mention
# ---------------------------------------------------------------------------


class CanCreateSourceMentionTests:
    def test_non_audiobook_source_always_allowed(self):
        source = {"family": "award"}
        assert can_create_source_mention(source, {}) is True
        assert can_create_source_mention(source, {"underlying_work_type": "podcast"}) is True

    def test_audiobook_ranking_allows_book_or_work(self):
        source = {"family": "audiobook ranking"}
        assert can_create_source_mention(source, {"underlying_work_type": "book"})
        assert can_create_source_mention(source, {"underlying_work_type": "work"})
        assert not can_create_source_mention(source, {"underlying_work_type": "podcast"})
        assert not can_create_source_mention(source, {"underlying_work_type": ""})

    def test_audiobook_ranking_case_insensitive(self):
        source = {"family": "  Audiobook Ranking  "}
        assert can_create_source_mention(source, {"underlying_work_type": "  Work  "})


# ---------------------------------------------------------------------------
# normalize_identity_text
# ---------------------------------------------------------------------------


class NormalizeIdentityTextTests:
    def test_lowercase_and_strip(self):
        assert normalize_identity_text("Hello World") == "hello world"

    def test_collapses_non_word_characters(self):
        assert normalize_identity_text("Hello, World!") == "hello world"
        assert normalize_identity_text("O’Brien’s Memoir") == "o brien s memoir"

    def test_unicode_handling(self):
        # Cyrillic characters are preserved as word characters
        assert "война" in normalize_identity_text("Война и мир")
        assert "мир" in normalize_identity_text("Война и мир")


# ---------------------------------------------------------------------------
# JSON I/O primitives
# ---------------------------------------------------------------------------


class JsonIOTests:
    def test_write_and_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.json"
            data = {"key": "value", "nested": {"a": 1}}
            write_json(path, data)
            result = read_json(path)
            assert result == data

    def test_read_json_returns_default_for_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"
            result = read_json(path, default={"fallback": True})
            assert result == {"fallback": True}

    def test_read_json_returns_none_for_missing_file_no_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"
            assert read_json(path) is None

    def test_read_json_or_default_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "alias.json"
            data = ["a", "b"]
            write_json(path, data)
            assert read_json_or_default(path, []) == data
            assert read_json_or_default(Path(tmp) / "nope.json", []) == []

    def test_write_json_preserves_missing_parent_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deep" / "nested" / "file.json"
            try:
                write_json(path, {"hello": "world"})
            except FileNotFoundError:
                pass
            else:
                raise AssertionError("write_json should not create missing parent directories")

    def test_write_json_trailing_newline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trailing.json"
            write_json(path, {"x": 1})
            text = path.read_text(encoding="utf-8")
            assert text.endswith("\n")

    def test_write_json_sorted_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sorted.json"
            write_json(path, {"b": 2, "a": 1})
            text = path.read_text(encoding="utf-8")
            assert text.index('"a"') < text.index('"b"')
